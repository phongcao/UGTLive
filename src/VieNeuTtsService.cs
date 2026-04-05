using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using NAudio.Wave;
using Application = System.Windows.Application;
using MessageBox = System.Windows.MessageBox;

namespace UGTLive
{
    public class VieNeuTtsService : ITtsService
    {
        private static VieNeuTtsService? _instance;
        private readonly HttpClient _httpClient;
        private readonly object _streamPlaybackLock = new object();
        private CancellationTokenSource? _activeStreamPlaybackCancellation;
        private WaveOutEvent? _activeStreamPlaybackPlayer;
        private Stream? _activeStreamPlaybackStream;
        private const int DefaultStreamingSampleRate = 24000;
        private const int StreamingStartupBufferMilliseconds = 1000;
        private const int StreamingReadBufferSize = 32768;

        public static readonly Dictionary<string, string> AvailableVoices = new Dictionary<string, string>
        {
            { "Xuân Vĩnh (Nam - Miền Nam)", "Xuân Vĩnh (Nam - Miền Nam)" },
            { "Bích Ngọc (Nữ - Miền Bắc)", "Bích Ngọc (Nữ - Miền Bắc)" },
            { "Phạm Tuyên (Nam - Miền Bắc)", "Phạm Tuyên (Nam - Miền Bắc)" },
            { "Thục Đoan (Nữ - Miền Nam)", "Thục Đoan (Nữ - Miền Nam)" },
        };

        public static VieNeuTtsService Instance
        {
            get
            {
                if (_instance == null)
                {
                    _instance = new VieNeuTtsService();
                }
                return _instance;
            }
        }

        private VieNeuTtsService()
        {
            _httpClient = new HttpClient();
            _httpClient.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            _httpClient.Timeout = TimeSpan.FromSeconds(120);
        }

        private string GetBaseUrl()
        {
            string url = ConfigManager.Instance.GetVieNeuTtsUrl();
            string port = ConfigManager.Instance.GetVieNeuTtsPort();
            return $"{url}:{port}";
        }

        public Task<bool> SpeakText(string text)
        {
            return SpeakTextInternalAsync(text, null, waitForCompletion: false, CancellationToken.None);
        }

        public Task<bool> SpeakTextAndWaitAsync(string text, string? voiceId = null, CancellationToken cancellationToken = default)
        {
            return SpeakTextInternalAsync(text, voiceId, waitForCompletion: true, cancellationToken);
        }

        public void StopActivePlayback()
        {
            lock (_streamPlaybackLock)
            {
                try
                {
                    _activeStreamPlaybackCancellation?.Cancel();
                }
                catch (Exception ex)
                {
                    Debug.WriteLine($"VieNeu-TTS stop cancellation failed: {ex.Message}");
                }

                try
                {
                    _activeStreamPlaybackPlayer?.Stop();
                }
                catch (Exception ex)
                {
                    Debug.WriteLine($"VieNeu-TTS stop player failed: {ex.Message}");
                }

                try
                {
                    _activeStreamPlaybackStream?.Dispose();
                }
                catch (Exception ex)
                {
                    Debug.WriteLine($"VieNeu-TTS stop stream dispose failed: {ex.Message}");
                }
            }
        }

        private async Task<bool> SpeakTextInternalAsync(string text, string? voiceId, bool waitForCompletion, CancellationToken cancellationToken)
        {
            try
            {
                Stopwatch speakStopwatch = Stopwatch.StartNew();

                if (cancellationToken.IsCancellationRequested)
                {
                    return false;
                }

                if (string.IsNullOrWhiteSpace(text))
                {
                    Debug.WriteLine("VieNeu-TTS: Cannot speak empty text");
                    return false;
                }

                string voice = string.IsNullOrWhiteSpace(voiceId)
                    ? ConfigManager.Instance.GetVieNeuTtsVoice()
                    : voiceId;

                Debug.WriteLine($"VieNeu-TTS: Sending TTS request for text: {text.Substring(0, Math.Min(50, text.Length))}...");

                if (await TrySpeakTextStreamAsync(text, voice, speakStopwatch, waitForCompletion, cancellationToken))
                {
                    Debug.WriteLine($"VieNeu-TTS: Streaming playback {(waitForCompletion ? "completed" : "started")} after {speakStopwatch.Elapsed.TotalSeconds:F2}s");
                    return true;
                }

                Debug.WriteLine($"VieNeu-TTS: Streaming unavailable, falling back to file download.");

                string? audioFile = await DownloadAudioFileAsync(text, voice, speakStopwatch);
                if (string.IsNullOrEmpty(audioFile))
                {
                    return false;
                }

                Debug.WriteLine($"VieNeu-TTS: Audio saved to {audioFile}, playing...");
                if (waitForCompletion)
                {
                    return await PlayAudioFileAndWaitAsync(audioFile, cancellationToken);
                }

                PlayAudioFile(audioFile);
                return true;
            }
            catch (HttpRequestException ex)
            {
                Debug.WriteLine($"VieNeu-TTS connection error: {ex.Message}");
                Application.Current.Dispatcher.Invoke(() =>
                {
                    MessageBox.Show(
                        "Cannot connect to VieNeu-TTS service. Please make sure it is installed and running from the Services tab.",
                        "VieNeu-TTS Not Available", MessageBoxButton.OK, MessageBoxImage.Warning);
                });
                return false;
            }
            catch (Exception ex)
            {
                Debug.WriteLine($"VieNeu-TTS error: {ex.Message}");
                return false;
            }
        }

        public async Task<string?> GenerateAudioFileAsync(string text, string voiceId)
        {
            try
            {
                if (string.IsNullOrWhiteSpace(text))
                {
                    Debug.WriteLine("VieNeu-TTS: Cannot generate audio for empty text");
                    return null;
                }

                string voice = string.IsNullOrWhiteSpace(voiceId)
                    ? ConfigManager.Instance.GetVieNeuTtsVoice()
                    : voiceId;

                return await DownloadAudioFileAsync(text, voice);
            }
            catch (HttpRequestException)
            {
                throw;
            }
            catch (Exception ex)
            {
                Debug.WriteLine($"VieNeu-TTS: Error generating audio: {ex.Message}");
                return null;
            }
        }

        private async Task<string?> DownloadAudioFileAsync(string text, string voice, Stopwatch? operationStopwatch = null)
        {
            // VieNeu-TTS web_stream.py uses GET /stream?text=...&voice_id=...
            // For longer text, use POST /stream with JSON body
            string url;
            HttpResponseMessage response;

            if (text.Length <= 500)
            {
                // Short text: use GET
                string encodedText = Uri.EscapeDataString(text);
                url = $"{GetBaseUrl()}/stream?text={encodedText}";
                if (!string.IsNullOrWhiteSpace(voice))
                {
                    url += $"&voice_id={Uri.EscapeDataString(voice)}";
                }
                response = await _httpClient.GetAsync(url);
            }
            else
            {
                // Long text: use POST
                url = $"{GetBaseUrl()}/stream";
                var requestData = new { text = text, voice_id = voice };
                string jsonRequest = JsonSerializer.Serialize(requestData);
                using var content = new StringContent(jsonRequest, Encoding.UTF8, "application/json");
                response = await _httpClient.PostAsync(url, content);
            }

            using (response)
            {
                if (!response.IsSuccessStatusCode)
                {
                    string errorContent = await response.Content.ReadAsStringAsync();
                    Debug.WriteLine($"VieNeu-TTS: Request failed: {response.StatusCode}. Details: {errorContent}");
                    throw new HttpRequestException($"VieNeu-TTS request failed: {response.StatusCode}. Details: {errorContent}")
                    {
                        Data = { ["StatusCode"] = response.StatusCode }
                    };
                }

                // The /stream endpoint returns audio/wav with a proper WAV header
                using Stream audioStream = await response.Content.ReadAsStreamAsync();

                string tempDir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "temp", "cache");
                Directory.CreateDirectory(tempDir);

                string audioFile = Path.Combine(tempDir, $"tts_vieneu_{DateTime.Now.Ticks}.wav");
                using (FileStream fileStream = File.Create(audioFile))
                {
                    await audioStream.CopyToAsync(fileStream);
                }

                Debug.WriteLine($"VieNeu-TTS: Audio file saved: {audioFile}");
                return audioFile;
            }
        }

        private async Task<bool> TrySpeakTextStreamAsync(string text, string voice, Stopwatch? operationStopwatch, bool waitForCompletion, CancellationToken cancellationToken)
        {
            HttpResponseMessage? response = null;

            try
            {
                // Use GET /stream for streaming playback
                string encodedText = Uri.EscapeDataString(text);
                string url = $"{GetBaseUrl()}/stream?text={encodedText}";
                if (!string.IsNullOrWhiteSpace(voice))
                {
                    url += $"&voice_id={Uri.EscapeDataString(voice)}";
                }

                using HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Get, url);
                response = await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken);

                if (response.StatusCode == HttpStatusCode.NotFound || response.StatusCode == HttpStatusCode.MethodNotAllowed)
                {
                    response.Dispose();
                    return false;
                }

                if (!response.IsSuccessStatusCode)
                {
                    string errorContent = await response.Content.ReadAsStringAsync();
                    Debug.WriteLine($"VieNeu-TTS: Streaming request failed: {response.StatusCode}. Details: {errorContent}");
                    response.Dispose();
                    return false;
                }

                // The VieNeu-TTS /stream endpoint returns audio/wav (WAV header + PCM16 data)
                // Read and play the WAV stream
                return await StartWavStreamingPlaybackAsync(response, operationStopwatch, waitForCompletion, cancellationToken);
            }
            catch (Exception ex)
            {
                response?.Dispose();
                Debug.WriteLine($"VieNeu-TTS: Streaming unavailable: {ex.Message}");
                return false;
            }
        }

        private async Task<bool> StartWavStreamingPlaybackAsync(HttpResponseMessage response, Stopwatch? operationStopwatch, bool waitForCompletion, CancellationToken cancellationToken)
        {
            CancellationTokenSource linkedCts;
            lock (_streamPlaybackLock)
            {
                _activeStreamPlaybackCancellation?.Cancel();
                _activeStreamPlaybackCancellation = new CancellationTokenSource();
                linkedCts = CancellationTokenSource.CreateLinkedTokenSource(_activeStreamPlaybackCancellation.Token, cancellationToken);
            }

            Stream? networkStream = null;
            WaveOutEvent? player = null;
            BufferedWaveProvider? bufferedProvider = null;

            try
            {
                networkStream = await response.Content.ReadAsStreamAsync();

                lock (_streamPlaybackLock)
                {
                    _activeStreamPlaybackStream = networkStream;
                }

                // The stream is a WAV file - use NAudio to read it
                // We'll buffer the initial WAV header + some data, then start playback
                using var memoryStream = new MemoryStream();
                byte[] buffer = new byte[StreamingReadBufferSize];
                int totalRead = 0;
                bool headerBuffered = false;

                // Read enough data to get the WAV header and initial audio
                while (totalRead < 44 + (DefaultStreamingSampleRate * 2)) // WAV header (44 bytes) + ~1 second of audio
                {
                    if (linkedCts.Token.IsCancellationRequested)
                    {
                        return false;
                    }

                    int bytesRead = await networkStream.ReadAsync(buffer, 0, buffer.Length, linkedCts.Token);
                    if (bytesRead == 0)
                    {
                        break; // End of stream
                    }

                    memoryStream.Write(buffer, 0, bytesRead);
                    totalRead += bytesRead;

                    if (!headerBuffered && totalRead >= 44)
                    {
                        headerBuffered = true;
                    }
                }

                // Read remaining data
                while (true)
                {
                    if (linkedCts.Token.IsCancellationRequested)
                    {
                        break;
                    }

                    int bytesRead = await networkStream.ReadAsync(buffer, 0, buffer.Length, linkedCts.Token);
                    if (bytesRead == 0)
                    {
                        break;
                    }

                    memoryStream.Write(buffer, 0, bytesRead);
                }

                if (memoryStream.Length < 44)
                {
                    Debug.WriteLine("VieNeu-TTS: Stream too short, no valid WAV data");
                    return false;
                }

                // Save to a temp file and play
                string tempDir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "temp", "cache");
                Directory.CreateDirectory(tempDir);
                string tempFile = Path.Combine(tempDir, $"tts_vieneu_stream_{DateTime.Now.Ticks}.wav");

                memoryStream.Position = 0;
                using (var fileStream = File.Create(tempFile))
                {
                    await memoryStream.CopyToAsync(fileStream);
                }

                if (waitForCompletion)
                {
                    return await PlayAudioFileAndWaitAsync(tempFile, linkedCts.Token);
                }
                else
                {
                    PlayAudioFile(tempFile);
                    return true;
                }
            }
            catch (OperationCanceledException)
            {
                Debug.WriteLine("VieNeu-TTS: Streaming playback cancelled");
                return false;
            }
            catch (Exception ex)
            {
                Debug.WriteLine($"VieNeu-TTS: Streaming playback error: {ex.Message}");
                return false;
            }
            finally
            {
                response.Dispose();
                linkedCts.Dispose();
            }
        }

        private void PlayAudioFile(string filePath)
        {
            try
            {
                var reader = new AudioFileReader(filePath);
                var player = new WaveOutEvent();
                player.Init(reader);
                player.PlaybackStopped += (s, e) =>
                {
                    player.Dispose();
                    reader.Dispose();
                };
                player.Play();
            }
            catch (Exception ex)
            {
                Debug.WriteLine($"VieNeu-TTS: Error playing audio file: {ex.Message}");
            }
        }

        private async Task<bool> PlayAudioFileAndWaitAsync(string filePath, CancellationToken cancellationToken)
        {
            var tcs = new TaskCompletionSource<bool>();
            AudioFileReader? reader = null;
            WaveOutEvent? player = null;

            try
            {
                reader = new AudioFileReader(filePath);
                player = new WaveOutEvent();
                player.Init(reader);

                using var registration = cancellationToken.Register(() =>
                {
                    try { player?.Stop(); } catch { }
                });

                player.PlaybackStopped += (s, e) =>
                {
                    tcs.TrySetResult(e.Exception == null);
                };

                player.Play();

                lock (_streamPlaybackLock)
                {
                    _activeStreamPlaybackPlayer = player;
                }

                return await tcs.Task;
            }
            catch (Exception ex)
            {
                Debug.WriteLine($"VieNeu-TTS: Error playing audio file: {ex.Message}");
                tcs.TrySetResult(false);
                return false;
            }
            finally
            {
                lock (_streamPlaybackLock)
                {
                    if (_activeStreamPlaybackPlayer == player)
                    {
                        _activeStreamPlaybackPlayer = null;
                    }
                }
                player?.Dispose();
                reader?.Dispose();
            }
        }
    }
}

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
    public class VieNeuGgufTtsService : ITtsService
    {
        private static VieNeuGgufTtsService? _instance;
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

        public static VieNeuGgufTtsService Instance
        {
            get
            {
                if (_instance == null)
                {
                    _instance = new VieNeuGgufTtsService();
                }
                return _instance;
            }
        }

        private VieNeuGgufTtsService()
        {
            _httpClient = new HttpClient();
            _httpClient.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            _httpClient.Timeout = TimeSpan.FromSeconds(300);
        }

        private string GetBaseUrl()
        {
            string url = ConfigManager.Instance.GetVieNeuGgufTtsUrl();
            string port = ConfigManager.Instance.GetVieNeuGgufTtsPort();
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
                    Debug.WriteLine($"VieNeu-GGUF-TTS stop cancellation failed: {ex.Message}");
                }

                try
                {
                    _activeStreamPlaybackPlayer?.Stop();
                }
                catch (Exception ex)
                {
                    Debug.WriteLine($"VieNeu-GGUF-TTS stop player failed: {ex.Message}");
                }

                try
                {
                    _activeStreamPlaybackStream?.Dispose();
                }
                catch (Exception ex)
                {
                    Debug.WriteLine($"VieNeu-GGUF-TTS stop stream dispose failed: {ex.Message}");
                }
            }
        }

        private async Task<bool> SpeakTextInternalAsync(string text, string? voiceId, bool waitForCompletion, CancellationToken cancellationToken)
        {
            IDisposable? gateLease = LocalTtsRequestGate.TryAcquire("VieNeu-GGUF-TTS");
            if (gateLease == null)
            {
                Debug.WriteLine("VieNeu-GGUF-TTS: Ignoring synthesis request because the service is already busy");
                return false;
            }

            try
            {
                Stopwatch speakStopwatch = Stopwatch.StartNew();

                if (cancellationToken.IsCancellationRequested)
                {
                    return false;
                }

                if (string.IsNullOrWhiteSpace(text))
                {
                    Debug.WriteLine("VieNeu-GGUF-TTS: Cannot speak empty text");
                    return false;
                }

                string voice = string.IsNullOrWhiteSpace(voiceId)
                    ? ConfigManager.Instance.GetVieNeuGgufTtsVoice()
                    : voiceId;

                Debug.WriteLine($"VieNeu-GGUF-TTS: Sending TTS request for text: {text.Substring(0, Math.Min(50, text.Length))}...");

                if (await TrySpeakTextStreamAsync(text, voice, speakStopwatch, waitForCompletion, cancellationToken))
                {
                    Debug.WriteLine($"VieNeu-GGUF-TTS: Streaming playback {(waitForCompletion ? "completed" : "started")} after {speakStopwatch.Elapsed.TotalSeconds:F2}s");
                    return true;
                }

                Debug.WriteLine($"VieNeu-GGUF-TTS: Streaming unavailable, falling back to file download.");

                string? audioFile = await DownloadAudioFileAsync(text, voice, speakStopwatch);
                if (string.IsNullOrEmpty(audioFile))
                {
                    return false;
                }

                Debug.WriteLine($"VieNeu-GGUF-TTS: Audio saved to {audioFile}, playing...");
                if (waitForCompletion)
                {
                    return await PlayAudioFileAndWaitAsync(audioFile, cancellationToken);
                }

                PlayAudioFile(audioFile);
                return true;
            }
            catch (HttpRequestException ex)
            {
                Debug.WriteLine($"VieNeu-GGUF-TTS connection error: {ex.Message}");
                Application.Current.Dispatcher.Invoke(() =>
                {
                    MessageBox.Show(
                        "Cannot connect to VieNeu-GGUF-TTS service. Please make sure it is installed and running from the Services tab, and that LM Studio is running with the VieNeu GGUF model loaded.",
                        "VieNeu-GGUF-TTS Not Available", MessageBoxButton.OK, MessageBoxImage.Warning);
                });
                return false;
            }
            catch (Exception ex)
            {
                Debug.WriteLine($"VieNeu-GGUF-TTS error: {ex.Message}");
                return false;
            }
            finally
            {
                gateLease.Dispose();
            }
        }

        public async Task<string?> GenerateAudioFileAsync(string text, string voiceId)
        {
            IDisposable? gateLease = LocalTtsRequestGate.TryAcquire("VieNeu-GGUF-TTS");
            if (gateLease == null)
            {
                Debug.WriteLine("VieNeu-GGUF-TTS: Ignoring audio generation request because the service is already busy");
                return null;
            }

            try
            {
                if (string.IsNullOrWhiteSpace(text))
                {
                    Debug.WriteLine("VieNeu-GGUF-TTS: Cannot generate audio for empty text");
                    return null;
                }

                string voice = string.IsNullOrWhiteSpace(voiceId)
                    ? ConfigManager.Instance.GetVieNeuGgufTtsVoice()
                    : voiceId;

                return await DownloadAudioFileAsync(text, voice);
            }
            catch (HttpRequestException)
            {
                throw;
            }
            catch (Exception ex)
            {
                Debug.WriteLine($"VieNeu-GGUF-TTS: Error generating audio: {ex.Message}");
                return null;
            }
            finally
            {
                gateLease.Dispose();
            }
        }

        private async Task<string?> DownloadAudioFileAsync(string text, string voice, Stopwatch? operationStopwatch = null)
        {
            string url;
            HttpResponseMessage response;

            if (text.Length <= 500)
            {
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
                    Debug.WriteLine($"VieNeu-GGUF-TTS: Request failed: {response.StatusCode}. Details: {errorContent}");
                    throw new HttpRequestException($"VieNeu-GGUF-TTS request failed: {response.StatusCode}. Details: {errorContent}")
                    {
                        Data = { ["StatusCode"] = response.StatusCode }
                    };
                }

                using Stream audioStream = await response.Content.ReadAsStreamAsync();

                string tempDir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "temp", "cache");
                Directory.CreateDirectory(tempDir);

                string audioFile = Path.Combine(tempDir, $"tts_vieneugguf_{DateTime.Now.Ticks}.wav");
                using (FileStream fileStream = File.Create(audioFile))
                {
                    await audioStream.CopyToAsync(fileStream);
                }

                Debug.WriteLine($"VieNeu-GGUF-TTS: Audio file saved: {audioFile}");
                return audioFile;
            }
        }

        private async Task<bool> TrySpeakTextStreamAsync(string text, string voice, Stopwatch? operationStopwatch, bool waitForCompletion, CancellationToken cancellationToken)
        {
            HttpResponseMessage? response = null;

            try
            {
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
                    Debug.WriteLine($"VieNeu-GGUF-TTS: Streaming request failed: {response.StatusCode}. Details: {errorContent}");
                    response.Dispose();
                    return false;
                }

                return await StartWavStreamingPlaybackAsync(response, operationStopwatch, waitForCompletion, cancellationToken);
            }
            catch (Exception ex)
            {
                response?.Dispose();
                Debug.WriteLine($"VieNeu-GGUF-TTS: Streaming unavailable: {ex.Message}");
                return false;
            }
        }

        private async Task<bool> StartWavStreamingPlaybackAsync(HttpResponseMessage response, Stopwatch? operationStopwatch, bool waitForCompletion, CancellationToken cancellationToken)
        {
            TaskCompletionSource<bool> playbackStarted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            TaskCompletionSource<bool> playbackCompleted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            int startupBufferBytes = DefaultStreamingSampleRate * 2 * StreamingStartupBufferMilliseconds / 1000;
            CancellationTokenSource playbackCancellation;

            lock (_streamPlaybackLock)
            {
                _activeStreamPlaybackCancellation?.Cancel();
                _activeStreamPlaybackCancellation = new CancellationTokenSource();
                playbackCancellation = CancellationTokenSource.CreateLinkedTokenSource(_activeStreamPlaybackCancellation.Token, cancellationToken);
            }

            _ = Task.Run(async () =>
            {
                BufferedWaveProvider? bufferedProvider = null;
                WaveOutEvent? wavePlayer = null;
                Stream? audioStream = null;
                long totalBytesRead = 0;
                bool hasStartedPlayback = false;

                try
                {
                    audioStream = await response.Content.ReadAsStreamAsync();

                    lock (_streamPlaybackLock)
                    {
                        _activeStreamPlaybackStream = audioStream;
                    }

                    // Read and skip the 44-byte WAV header
                    byte[] wavHeader = new byte[44];
                    int headerRead = 0;
                    while (headerRead < 44)
                    {
                        int n = await audioStream.ReadAsync(wavHeader, headerRead, 44 - headerRead, playbackCancellation.Token);
                        if (n == 0) break;
                        headerRead += n;
                    }

                    if (headerRead < 44)
                    {
                        Debug.WriteLine("VieNeu-GGUF-TTS: Stream too short, no valid WAV header");
                        playbackStarted.TrySetResult(false);
                        return;
                    }

                    bufferedProvider = new BufferedWaveProvider(new WaveFormat(DefaultStreamingSampleRate, 16, 1))
                    {
                        BufferDuration = TimeSpan.FromSeconds(30),
                        DiscardOnBufferOverflow = true,
                        ReadFully = true
                    };

                    wavePlayer = new WaveOutEvent();
                    wavePlayer.Init(bufferedProvider);

                    lock (_streamPlaybackLock)
                    {
                        _activeStreamPlaybackPlayer = wavePlayer;
                    }

                    byte[] readBuffer = new byte[StreamingReadBufferSize];

                    while (true)
                    {
                        int bytesRead = await audioStream.ReadAsync(readBuffer, 0, readBuffer.Length, playbackCancellation.Token);
                        if (bytesRead <= 0)
                        {
                            Debug.WriteLine($"VieNeu-GGUF-TTS: Stream EOF, total_bytes={totalBytesRead}, playback_started={hasStartedPlayback}");
                            break;
                        }

                        totalBytesRead += bytesRead;
                        bufferedProvider.AddSamples(readBuffer, 0, bytesRead);

                        if (!hasStartedPlayback && bufferedProvider.BufferedBytes >= startupBufferBytes)
                        {
                            Debug.WriteLine($"VieNeu-GGUF-TTS: Starting streaming playback, buffered={bufferedProvider.BufferedBytes} bytes, elapsed={operationStopwatch?.Elapsed.TotalSeconds:F2}s");
                            wavePlayer.Play();
                            hasStartedPlayback = true;
                            playbackStarted.TrySetResult(true);
                        }
                    }

                    // If we never hit the buffer threshold, start playback with whatever we have
                    if (!hasStartedPlayback)
                    {
                        if (bufferedProvider.BufferedBytes == 0)
                        {
                            Debug.WriteLine("VieNeu-GGUF-TTS: Stream ended without playable audio");
                            playbackStarted.TrySetResult(false);
                            return;
                        }

                        Debug.WriteLine($"VieNeu-GGUF-TTS: Starting playback at stream end, buffered={bufferedProvider.BufferedBytes} bytes");
                        wavePlayer.Play();
                        hasStartedPlayback = true;
                        playbackStarted.TrySetResult(true);
                    }

                    // Wait for buffered audio to finish playing
                    while (bufferedProvider.BufferedBytes > 0)
                    {
                        await Task.Delay(50);
                    }

                    wavePlayer.Stop();
                    Debug.WriteLine($"VieNeu-GGUF-TTS: Streaming playback completed, total_bytes={totalBytesRead}, elapsed={operationStopwatch?.Elapsed.TotalSeconds:F2}s");
                    playbackCompleted.TrySetResult(true);
                }
                catch (OperationCanceledException)
                {
                    Debug.WriteLine("VieNeu-GGUF-TTS: Streaming playback cancelled");
                    if (!hasStartedPlayback) playbackStarted.TrySetResult(false);
                    playbackCompleted.TrySetResult(false);
                }
                catch (Exception ex)
                {
                    Debug.WriteLine($"VieNeu-GGUF-TTS: Streaming playback error: {ex.Message}");
                    if (!hasStartedPlayback) playbackStarted.TrySetResult(false);
                    playbackCompleted.TrySetResult(false);
                }
                finally
                {
                    lock (_streamPlaybackLock)
                    {
                        _activeStreamPlaybackPlayer = null;
                        _activeStreamPlaybackStream = null;
                    }
                    wavePlayer?.Dispose();
                    audioStream?.Dispose();
                    response.Dispose();
                    playbackCancellation.Dispose();
                }
            });

            bool started = await playbackStarted.Task;
            if (!waitForCompletion || !started)
            {
                return started;
            }

            return await playbackCompleted.Task;
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
                Debug.WriteLine($"VieNeu-GGUF-TTS: Error playing audio file: {ex.Message}");
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
                Debug.WriteLine($"VieNeu-GGUF-TTS: Error playing audio file: {ex.Message}");
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

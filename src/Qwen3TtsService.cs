using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Net;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using NAudio.Wave;
using Application = System.Windows.Application;
using Console = UGTLive.Qwen3TtsDebugConsole;
using MessageBox = System.Windows.MessageBox;

namespace UGTLive
{
    internal static class Qwen3TtsDebugConsole
    {
        private static readonly object FileLock = new object();
        private static readonly string LogPath = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "qwen3_tts_client_log.txt");
        private static bool _sessionHeaderWritten = false;

        public static void WriteLine(string? message)
        {
            if (message == null)
            {
                return;
            }

            Debug.WriteLine(message);
            System.Console.WriteLine(message);
            AppendToFile(message);
        }

        private static void AppendToFile(string message)
        {
            try
            {
                lock (FileLock)
                {
                    if (!_sessionHeaderWritten)
                    {
                        int pid = Process.GetCurrentProcess().Id;
                        File.AppendAllText(
                            LogPath,
                            $"{Environment.NewLine}=== Qwen3TtsService Session {DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} (PID {pid}) ==={Environment.NewLine}"
                        );
                        _sessionHeaderWritten = true;
                    }

                    File.AppendAllText(
                        LogPath,
                        $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff}] {message}{Environment.NewLine}"
                    );
                }
            }
            catch
            {
                // Ignore file logging failures so TTS logging never affects playback.
            }
        }
    }

    public class Qwen3TtsService : ITtsService
    {
        private static Qwen3TtsService? _instance;
        private readonly HttpClient _httpClient;
        private readonly object _streamPlaybackLock = new object();
        private CancellationTokenSource? _activeStreamPlaybackCancellation;
        private WaveOutEvent? _activeStreamPlaybackPlayer;
        private Stream? _activeStreamPlaybackStream;
        private const string DefaultVoice = "ono_anna";
        private const int DefaultStreamingSampleRate = 24000;
        private const int StreamingStartupBufferMilliseconds = 1000;
        private const int FastModeStreamingStartupBufferMilliseconds = 250;
        private const int StreamingReadBufferSize = 32768;

        public static readonly Dictionary<string, string> AvailableVoices = new Dictionary<string, string>
        {
            { "Ono Anna (Japanese)", "ono_anna" },
            { "Ryan (English)", "ryan" },
            { "Aiden (English)", "aiden" },
            { "Vivian (Chinese)", "vivian" },
            { "Serena (Chinese)", "serena" },
            { "Uncle Fu (Chinese)", "uncle_fu" },
            { "Dylan (Chinese)", "dylan" },
            { "Eric (Chinese)", "eric" },
            { "Sohee (Korean)", "sohee" },
        };

        public static Qwen3TtsService Instance
        {
            get
            {
                if (_instance == null)
                {
                    _instance = new Qwen3TtsService();
                }
                return _instance;
            }
        }

        private Qwen3TtsService()
        {
            _httpClient = new HttpClient();
            _httpClient.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
            _httpClient.Timeout = TimeSpan.FromSeconds(120);
        }

        private string GetBaseUrl()
        {
            string url = ConfigManager.Instance.GetQwen3TtsUrl();
            string port = ConfigManager.Instance.GetQwen3TtsPort();
            return $"{url}:{port}";
        }

        private string GetExternalSpeechUrl()
        {
            string apiBase = ConfigManager.Instance.GetQwen3TtsExternalApiBase().Trim().TrimEnd('/');
            if (!apiBase.EndsWith("/v1", StringComparison.OrdinalIgnoreCase))
            {
                apiBase += "/v1";
            }

            return $"{apiBase}/audio/speech";
        }

        private bool IsLocalBackend()
        {
            return ConfigManager.Instance.IsQwen3TtsLocalBackend();
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
                    Console.WriteLine($"Qwen3-TTS stop cancellation failed: {ex.Message}");
                }

                try
                {
                    _activeStreamPlaybackPlayer?.Stop();
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Qwen3-TTS stop player failed: {ex.Message}");
                }

                try
                {
                    _activeStreamPlaybackStream?.Dispose();
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Qwen3-TTS stop stream dispose failed: {ex.Message}");
                }
            }
        }

        private async Task<bool> SpeakTextInternalAsync(string text, string? voiceId, bool waitForCompletion, CancellationToken cancellationToken)
        {
            IDisposable? gateLease = null;
            if (IsLocalBackend())
            {
                gateLease = LocalTtsRequestGate.TryAcquire("Qwen3-TTS");
                if (gateLease == null)
                {
                    Console.WriteLine("Qwen3-TTS: Ignoring synthesis request because the local service is already busy");
                    return false;
                }
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
                    Console.WriteLine("Cannot speak empty text");
                    return false;
                }

                string voice = string.IsNullOrWhiteSpace(voiceId)
                    ? ConfigManager.Instance.GetQwen3TtsVoice()
                    : voiceId;
                if (string.IsNullOrWhiteSpace(voice))
                {
                    voice = DefaultVoice;
                }

                Console.WriteLine($"Sending TTS request to Qwen3-TTS ({(IsLocalBackend() ? "local" : "external")}) for text: {text.Substring(0, Math.Min(50, text.Length))}...");

                if (await TrySpeakTextStreamAsync(text, voice, speakStopwatch, waitForCompletion, cancellationToken))
                {
                    if (waitForCompletion)
                    {
                        Console.WriteLine($"Qwen3-TTS streaming playback completed after {FormatElapsed(speakStopwatch)}");
                    }
                    else
                    {
                        Console.WriteLine($"Qwen3-TTS streaming playback started after {FormatElapsed(speakStopwatch)}");
                    }
                    return true;
                }

                Console.WriteLine($"Qwen3-TTS streaming path did not start playback after {FormatElapsed(speakStopwatch)}; falling back to file download.");

                string? audioFile = await DownloadAudioFileAsync(text, voice, speakStopwatch);
                if (string.IsNullOrEmpty(audioFile))
                {
                    return false;
                }

                Console.WriteLine($"Audio saved to {audioFile} after {FormatElapsed(speakStopwatch)}, playing...");
                if (waitForCompletion)
                {
                    return await PlayAudioFileAndWaitAsync(audioFile, cancellationToken);
                }

                PlayAudioFile(audioFile);
                return true;
            }
            catch (HttpRequestException ex)
            {
                Console.WriteLine($"Qwen3-TTS connection error: {ex.Message}");
                Application.Current.Dispatcher.Invoke(() =>
                {
                    string message = IsLocalBackend()
                        ? "Cannot connect to Qwen3-TTS service. Please make sure it is installed and running from the Services tab."
                        : "Cannot connect to the external Qwen3-TTS endpoint. Please verify qwen3_tts_external_api_base, qwen3_tts_external_model, and qwen3_tts_external_api_key in config.txt.";
                    MessageBox.Show(message,
                        "Qwen3-TTS Not Available", MessageBoxButton.OK, MessageBoxImage.Warning);
                });
                return false;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Error initiating Qwen3-TTS: {ex.Message}");
                return false;
            }
            finally
            {
                gateLease?.Dispose();
            }
        }

        public async Task<string?> GenerateAudioFileAsync(string text, string voiceId)
        {
            IDisposable? gateLease = null;
            if (IsLocalBackend())
            {
                gateLease = LocalTtsRequestGate.TryAcquire("Qwen3-TTS");
                if (gateLease == null)
                {
                    Console.WriteLine("Qwen3-TTS: Ignoring audio generation request because the local service is already busy");
                    return null;
                }
            }

            try
            {
                if (string.IsNullOrWhiteSpace(text))
                {
                    Console.WriteLine("Cannot generate audio for empty text");
                    return null;
                }

                string voice = voiceId;
                if (string.IsNullOrWhiteSpace(voice))
                {
                    voice = DefaultVoice;
                }

                if (ConfigManager.Instance.GetLogExtraDebugStuff())
                {
                    Console.WriteLine($"Generating Qwen3-TTS audio for text: {text.Substring(0, Math.Min(50, text.Length))}...");
                }

                return await DownloadAudioFileAsync(text, voice);
            }
            catch (HttpRequestException)
            {
                throw;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Error initiating Qwen3-TTS audio generation: {ex.Message}");
                return null;
            }
            finally
            {
                gateLease?.Dispose();
            }
        }

        private async Task<string?> DownloadAudioFileAsync(string text, string voice, Stopwatch? operationStopwatch = null)
        {
            Stopwatch requestStopwatch = operationStopwatch ?? Stopwatch.StartNew();

            Console.WriteLine($"Qwen3-TTS file request started: backend={(IsLocalBackend() ? "local" : "external")}, voice={voice}");

            using HttpResponseMessage response = IsLocalBackend()
                ? await SendLocalDownloadRequestAsync(text, voice)
                : await SendExternalDownloadRequestAsync(text, voice);

            string contentType = response.Content.Headers.ContentType?.MediaType ?? "unknown";
            long? contentLength = response.Content.Headers.ContentLength;

            Console.WriteLine(
                $"Qwen3-TTS file response headers received after {FormatElapsed(requestStopwatch)}: " +
                $"status={(int)response.StatusCode} {response.StatusCode}, content_type={contentType}, content_length={DescribeContentLength(contentLength)}"
            );

            if (!response.IsSuccessStatusCode)
            {
                string errorContent = await response.Content.ReadAsStringAsync();
                Console.WriteLine($"Qwen3-TTS request failed after {FormatElapsed(requestStopwatch)}: {response.StatusCode}. Details: {errorContent}");

                throw new HttpRequestException($"Qwen3-TTS request failed: {response.StatusCode}. Details: {errorContent}")
                {
                    Data = { ["StatusCode"] = response.StatusCode }
                };
            }

            if (ConfigManager.Instance.GetLogExtraDebugStuff())
            {
                Console.WriteLine($"Qwen3-TTS request successful after {FormatElapsed(requestStopwatch)}, content type: {contentType}");
            }

            using Stream audioStream = await response.Content.ReadAsStreamAsync();

            string tempDir = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "temp", "cache");
            Directory.CreateDirectory(tempDir);

            string extension = ".wav";
            if (contentType.Contains("audio/mpeg") || contentType.Contains("audio/mp3"))
            {
                extension = ".mp3";
            }
            else if (contentType.Contains("audio/ogg"))
            {
                extension = ".ogg";
            }

            string audioFile = Path.Combine(tempDir, $"tts_qwen3_{DateTime.Now.Ticks}{extension}");
            long writtenBytes;
            using (FileStream fileStream = File.Create(audioFile))
            {
                await audioStream.CopyToAsync(fileStream);
                writtenBytes = fileStream.Length;
            }

            Console.WriteLine(
                $"Qwen3-TTS audio file generated after {FormatElapsed(requestStopwatch)}: {audioFile} " +
                $"({DescribeContentLength(writtenBytes)})"
            );
            return audioFile;
        }

        private async Task<HttpResponseMessage> SendLocalDownloadRequestAsync(string text, string voice)
        {
            string url = $"{GetBaseUrl()}/tts";
            bool fastMode = ConfigManager.Instance.GetQwen3TtsFastMode();
            using StringContent content = CreateLocalRequestContent(text, voice, fastMode);
            return await _httpClient.PostAsync(url, content);
        }

        private async Task<HttpResponseMessage> SendExternalDownloadRequestAsync(string text, string voice)
        {
            string model = ConfigManager.Instance.GetQwen3TtsExternalModel().Trim();
            if (string.IsNullOrWhiteSpace(model))
            {
                throw new HttpRequestException("Qwen3-TTS external model is not configured. Set qwen3_tts_external_model in config.txt.");
            }

            using HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, GetExternalSpeechUrl())
            {
                Content = CreateExternalRequestContent(text, voice, model)
            };

            string apiKey = ConfigManager.Instance.GetQwen3TtsExternalApiKey().Trim();
            if (!string.IsNullOrWhiteSpace(apiKey))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);
            }

            return await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);
        }

        private StringContent CreateLocalRequestContent(string text, string voice, bool fastMode)
        {
            var requestData = new
            {
                text = text,
                voice = voice,
                fast_mode = fastMode
            };

            string jsonRequest = JsonSerializer.Serialize(requestData);
            return new StringContent(jsonRequest, Encoding.UTF8, "application/json");
        }

        private StringContent CreateExternalRequestContent(string text, string voice, string model)
        {
            var requestData = new
            {
                model = model,
                input = text,
                voice = voice,
                response_format = "mp3"
            };

            string jsonRequest = JsonSerializer.Serialize(requestData);
            return new StringContent(jsonRequest, Encoding.UTF8, "application/json");
        }

        private StringContent CreateExternalStreamingRequestContent(string text, string voice, string model)
        {
            var requestData = new
            {
                model = model,
                input = text,
                voice = voice,
                response_format = "pcm",
                stream = true
            };

            string jsonRequest = JsonSerializer.Serialize(requestData);
            return new StringContent(jsonRequest, Encoding.UTF8, "application/json");
        }

        private async Task<bool> TrySpeakTextStreamAsync(string text, string voice, Stopwatch? operationStopwatch, bool waitForCompletion, CancellationToken cancellationToken)
        {
            if (!IsLocalBackend())
            {
                return await TrySpeakExternalStreamAsync(text, voice, operationStopwatch, waitForCompletion, cancellationToken);
            }

            string url = $"{GetBaseUrl()}/tts/stream";
            HttpResponseMessage? response = null;
            bool fastMode = ConfigManager.Instance.GetQwen3TtsFastMode();
            Stopwatch requestStopwatch = operationStopwatch ?? Stopwatch.StartNew();

            Console.WriteLine($"Qwen3-TTS local streaming request started: url={url}, voice={voice}, fast_mode={fastMode}");

            try
            {
                using HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, url)
                {
                    Content = CreateLocalRequestContent(text, voice, fastMode)
                };

                response = await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);

                Console.WriteLine(
                    $"Qwen3-TTS local streaming response headers received after {FormatElapsed(requestStopwatch)}: " +
                    $"status={(int)response.StatusCode} {response.StatusCode}"
                );

                if (response.StatusCode == HttpStatusCode.NotFound || response.StatusCode == HttpStatusCode.MethodNotAllowed)
                {
                    Console.WriteLine(
                        $"Qwen3-TTS local streaming endpoint unavailable after {FormatElapsed(requestStopwatch)} " +
                        $"({response.StatusCode}); falling back to file playback."
                    );
                    response.Dispose();
                    return false;
                }

                if (!response.IsSuccessStatusCode)
                {
                    string errorContent = await response.Content.ReadAsStringAsync();
                    Console.WriteLine(
                        $"Qwen3-TTS streaming request failed after {FormatElapsed(requestStopwatch)}: {response.StatusCode}. " +
                        $"Falling back to file playback. Details: {errorContent}"
                    );
                    response.Dispose();
                    return false;
                }

                string audioFormat = GetResponseHeaderValue(response, "X-Audio-Format") ?? "pcm16le";
                if (!string.Equals(audioFormat, "pcm16le", StringComparison.OrdinalIgnoreCase))
                {
                    Console.WriteLine($"Unsupported Qwen3-TTS streaming audio format '{audioFormat}', falling back to file playback.");
                    response.Dispose();
                    return false;
                }

                int sampleRate = GetStreamingSampleRate(response);
                Console.WriteLine(
                    $"Qwen3-TTS local streaming accepted after {FormatElapsed(requestStopwatch)}: " +
                    $"audio_format={audioFormat}, sample_rate={sampleRate}, content_length={DescribeContentLength(response.Content.Headers.ContentLength)}"
                );

                return await StartStreamingPlaybackAsync(response, sampleRate, fastMode, requestStopwatch, "local", waitForCompletion, cancellationToken);
            }
            catch (Exception ex)
            {
                response?.Dispose();
                Console.WriteLine(
                    $"Qwen3-TTS streaming playback unavailable after {FormatElapsed(requestStopwatch)}; " +
                    $"falling back to file playback: {ex.Message}"
                );
                return false;
            }
        }

        private async Task<bool> TrySpeakExternalStreamAsync(string text, string voice, Stopwatch? operationStopwatch, bool waitForCompletion, CancellationToken cancellationToken)
        {
            HttpResponseMessage? response = null;
            Stopwatch requestStopwatch = operationStopwatch ?? Stopwatch.StartNew();

            Console.WriteLine($"Qwen3-TTS external streaming request started: url={GetExternalSpeechUrl()}, voice={voice}, model={ConfigManager.Instance.GetQwen3TtsExternalModel().Trim()}");

            try
            {
                response = await SendExternalStreamingRequestAsync(text, voice);

                Console.WriteLine(
                    $"Qwen3-TTS external streaming response headers received after {FormatElapsed(requestStopwatch)}: " +
                    $"status={(int)response.StatusCode} {response.StatusCode}"
                );

                if (!response.IsSuccessStatusCode)
                {
                    string errorContent = await response.Content.ReadAsStringAsync();
                    Console.WriteLine(
                        $"Qwen3-TTS external streaming request failed after {FormatElapsed(requestStopwatch)}: {response.StatusCode}. " +
                        $"Falling back to file playback. Details: {errorContent}"
                    );
                    response.Dispose();
                    return false;
                }

                string contentType = response.Content.Headers.ContentType?.MediaType ?? "unknown";
                if (!string.Equals(contentType, "audio/pcm", StringComparison.OrdinalIgnoreCase) &&
                    !string.Equals(contentType, "audio/pcm16", StringComparison.OrdinalIgnoreCase) &&
                    !string.Equals(contentType, "audio/l16", StringComparison.OrdinalIgnoreCase))
                {
                    Console.WriteLine($"Unsupported Qwen3-TTS external streaming content type '{contentType}', falling back to file playback.");
                    response.Dispose();
                    return false;
                }

                Console.WriteLine(
                    $"Qwen3-TTS external streaming accepted after {FormatElapsed(requestStopwatch)}: " +
                    $"content_type={contentType}, sample_rate={DefaultStreamingSampleRate}, content_length={DescribeContentLength(response.Content.Headers.ContentLength)}"
                );

                return await StartStreamingPlaybackAsync(response, DefaultStreamingSampleRate, true, requestStopwatch, "external", waitForCompletion, cancellationToken);
            }
            catch (Exception ex)
            {
                response?.Dispose();
                Console.WriteLine(
                    $"Qwen3-TTS external streaming playback unavailable after {FormatElapsed(requestStopwatch)}; " +
                    $"falling back to file playback: {ex.Message}"
                );
                return false;
            }
        }

        private async Task<HttpResponseMessage> SendExternalStreamingRequestAsync(string text, string voice)
        {
            string model = ConfigManager.Instance.GetQwen3TtsExternalModel().Trim();
            if (string.IsNullOrWhiteSpace(model))
            {
                throw new HttpRequestException("Qwen3-TTS external model is not configured. Set qwen3_tts_external_model in config.txt.");
            }

            using HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, GetExternalSpeechUrl())
            {
                Content = CreateExternalStreamingRequestContent(text, voice, model)
            };

            string apiKey = ConfigManager.Instance.GetQwen3TtsExternalApiKey().Trim();
            if (!string.IsNullOrWhiteSpace(apiKey))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey);
            }

            return await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead);
        }

        private async Task<bool> StartStreamingPlaybackAsync(HttpResponseMessage response, int sampleRate, bool fastMode, Stopwatch requestStopwatch, string streamSource, bool waitForCompletion, CancellationToken cancellationToken)
        {
            TaskCompletionSource<bool> playbackStarted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            TaskCompletionSource<bool> playbackCompleted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            int startupBufferMilliseconds = fastMode ? FastModeStreamingStartupBufferMilliseconds : StreamingStartupBufferMilliseconds;
            int startupBufferBytes = sampleRate * 2 * startupBufferMilliseconds / 1000;
            CancellationTokenSource playbackCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);

            Console.WriteLine(
                $"Qwen3-TTS {streamSource} streaming playback setup: sample_rate={sampleRate}, fast_mode={fastMode}, " +
                $"startup_buffer_ms={startupBufferMilliseconds}, startup_buffer_bytes={startupBufferBytes}, read_buffer_bytes={StreamingReadBufferSize}"
            );

            _ = Task.Run(async () =>
            {
                BufferedWaveProvider? bufferedProvider = null;
                WaveOutEvent? wavePlayer = null;
                Stream? audioStream = null;
                long totalBytesRead = 0;
                int readCount = 0;
                bool receivedAnyAudio = false;
                bool hasStartedPlayback = false;

                try
                {
                    audioStream = await response.Content.ReadAsStreamAsync();
                    Console.WriteLine($"Qwen3-TTS {streamSource} audio stream opened after {FormatElapsed(requestStopwatch)}");

                    bufferedProvider = new BufferedWaveProvider(new WaveFormat(sampleRate, 16, 1))
                    {
                        BufferDuration = TimeSpan.FromSeconds(30),
                        DiscardOnBufferOverflow = true,
                        ReadFully = true
                    };

                    wavePlayer = new WaveOutEvent();
                    wavePlayer.Init(bufferedProvider);
                    RegisterActiveStreamingPlayback(playbackCancellation, wavePlayer, audioStream);

                    byte[] readBuffer = new byte[StreamingReadBufferSize];

                    while (true)
                    {
                        int bytesRead = await audioStream.ReadAsync(readBuffer.AsMemory(0, readBuffer.Length), playbackCancellation.Token);
                        if (bytesRead <= 0)
                        {
                            Console.WriteLine(
                                $"Qwen3-TTS {streamSource} stream reached EOF after {FormatElapsed(requestStopwatch)}: " +
                                $"reads={readCount}, total_bytes={totalBytesRead}, playback_started={hasStartedPlayback}"
                            );
                            break;
                        }

                        readCount++;
                        totalBytesRead += bytesRead;
                        bufferedProvider.AddSamples(readBuffer, 0, bytesRead);

                        if (!receivedAnyAudio)
                        {
                            receivedAnyAudio = true;
                            Console.WriteLine(
                                $"Qwen3-TTS {streamSource} first audio bytes received after {FormatElapsed(requestStopwatch)}: " +
                                $"bytes_read={bytesRead}, buffered_bytes={bufferedProvider.BufferedBytes}"
                            );
                        }

                        if (!hasStartedPlayback && bufferedProvider.BufferedBytes >= startupBufferBytes)
                        {
                            Console.WriteLine(
                                $"Starting Qwen3-TTS {streamSource} streaming playback after {FormatElapsed(requestStopwatch)}: " +
                                $"buffered_bytes={bufferedProvider.BufferedBytes}, threshold_bytes={startupBufferBytes}, " +
                                $"reads={readCount}, total_bytes={totalBytesRead}"
                            );
                            wavePlayer.Play();
                            hasStartedPlayback = true;
                            playbackStarted.TrySetResult(true);
                        }
                    }

                    if (!hasStartedPlayback)
                    {
                        if (bufferedProvider.BufferedBytes == 0)
                        {
                            Console.WriteLine($"Qwen3-TTS {streamSource} stream ended without playable audio after {FormatElapsed(requestStopwatch)}");
                            playbackStarted.TrySetResult(false);
                            return;
                        }

                        Console.WriteLine(
                            $"Starting Qwen3-TTS {streamSource} streaming playback after {FormatElapsed(requestStopwatch)} at stream end: " +
                            $"buffered_bytes={bufferedProvider.BufferedBytes}, reads={readCount}, total_bytes={totalBytesRead}"
                        );
                        wavePlayer.Play();
                        hasStartedPlayback = true;
                        playbackStarted.TrySetResult(true);
                    }

                    while (bufferedProvider.BufferedBytes > 0)
                    {
                        await Task.Delay(50);
                    }

                    wavePlayer.Stop();
                    Console.WriteLine(
                        $"Qwen3-TTS {streamSource} streaming playback completed successfully after {FormatElapsed(requestStopwatch)}: " +
                        $"reads={readCount}, total_bytes={totalBytesRead}"
                    );
                    playbackCompleted.TrySetResult(true);
                }
                catch (OperationCanceledException)
                {
                    Console.WriteLine($"Qwen3-TTS {streamSource} streaming playback canceled after {FormatElapsed(requestStopwatch)}");
                    if (!hasStartedPlayback)
                    {
                        playbackStarted.TrySetResult(false);
                    }
                    playbackCompleted.TrySetResult(false);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error during Qwen3-TTS {streamSource} streaming playback after {FormatElapsed(requestStopwatch)}: {ex.Message}");
                    if (!hasStartedPlayback)
                    {
                        playbackStarted.TrySetResult(false);
                    }
                    playbackCompleted.TrySetResult(false);
                }
                finally
                {
                    ClearActiveStreamingPlayback(playbackCancellation);
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

        private void RegisterActiveStreamingPlayback(CancellationTokenSource playbackCancellation, WaveOutEvent wavePlayer, Stream audioStream)
        {
            lock (_streamPlaybackLock)
            {
                _activeStreamPlaybackCancellation = playbackCancellation;
                _activeStreamPlaybackPlayer = wavePlayer;
                _activeStreamPlaybackStream = audioStream;
            }
        }

        private void ClearActiveStreamingPlayback(CancellationTokenSource playbackCancellation)
        {
            lock (_streamPlaybackLock)
            {
                if (!ReferenceEquals(_activeStreamPlaybackCancellation, playbackCancellation))
                {
                    return;
                }

                _activeStreamPlaybackCancellation = null;
                _activeStreamPlaybackPlayer = null;
                _activeStreamPlaybackStream = null;
            }
        }

        private int GetStreamingSampleRate(HttpResponseMessage response)
        {
            string? sampleRateHeader = GetResponseHeaderValue(response, "X-Audio-Sample-Rate");
            if (int.TryParse(sampleRateHeader, out int sampleRate) && sampleRate > 0)
            {
                return sampleRate;
            }

            return DefaultStreamingSampleRate;
        }

        private string? GetResponseHeaderValue(HttpResponseMessage response, string headerName)
        {
            if (!response.Headers.TryGetValues(headerName, out IEnumerable<string>? values))
            {
                return null;
            }

            foreach (string value in values)
            {
                if (!string.IsNullOrWhiteSpace(value))
                {
                    return value;
                }
            }

            return null;
        }

        private static string FormatElapsed(Stopwatch stopwatch)
        {
            return $"{stopwatch.Elapsed.TotalMilliseconds:F0}ms";
        }

        private static string DescribeContentLength(long? contentLength)
        {
            if (!contentLength.HasValue)
            {
                return "unknown";
            }

            return $"{contentLength.Value} bytes";
        }

        private async Task<bool> PlayAudioFileAndWaitAsync(string filePath, CancellationToken cancellationToken)
        {
            return await Task.Run(() =>
            {
                IWavePlayer? wavePlayer = null;
                AudioFileReader? audioFile = null;
                ManualResetEvent playbackFinished = new ManualResetEvent(false);

                try
                {
                    wavePlayer = new WaveOutEvent();
                    wavePlayer.PlaybackStopped += (sender, args) =>
                    {
                        playbackFinished.Set();
                    };

                    audioFile = new AudioFileReader(filePath);
                    wavePlayer.Init(audioFile);

                    Console.WriteLine($"Starting audio playback of file: {filePath}");
                    wavePlayer.Play();

                    while (!playbackFinished.WaitOne(100))
                    {
                        if (!cancellationToken.IsCancellationRequested)
                        {
                            continue;
                        }

                        wavePlayer.Stop();
                        return false;
                    }

                    wavePlayer.Stop();
                    Console.WriteLine("Audio playback completed successfully");
                    return true;
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error playing audio file: {ex.Message}");

                    Application.Current.Dispatcher.Invoke(() =>
                    {
                        MessageBox.Show($"Error playing audio: {ex.Message}",
                            "Audio Playback Error", MessageBoxButton.OK, MessageBoxImage.Warning);
                    });
                    return false;
                }
                finally
                {
                    wavePlayer?.Dispose();
                    audioFile?.Dispose();

                    try
                    {
                        if (File.Exists(filePath))
                        {
                            File.Delete(filePath);
                            Console.WriteLine($"Temp audio file deleted: {filePath}");
                        }
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"Failed to delete temp audio file: {ex.Message}");
                    }
                }
            }, cancellationToken);
        }

        private void PlayAudioFile(string filePath)
        {
            try
            {
                Task.Run(() =>
                {
                    IWavePlayer? wavePlayer = null;
                    AudioFileReader? audioFile = null;
                    ManualResetEvent playbackFinished = new ManualResetEvent(false);

                    try
                    {
                        wavePlayer = new WaveOutEvent();
                        wavePlayer.PlaybackStopped += (sender, args) =>
                        {
                            playbackFinished.Set();
                        };

                        audioFile = new AudioFileReader(filePath);

                        wavePlayer.Init(audioFile);

                        Console.WriteLine($"Starting audio playback of file: {filePath}");
                        wavePlayer.Play();

                        playbackFinished.WaitOne();

                        wavePlayer.Stop();

                        Console.WriteLine("Audio playback completed successfully");
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"Error playing audio file: {ex.Message}");

                        Application.Current.Dispatcher.Invoke(() =>
                        {
                            MessageBox.Show($"Error playing audio: {ex.Message}",
                                "Audio Playback Error", MessageBoxButton.OK, MessageBoxImage.Warning);
                        });
                    }
                    finally
                    {
                        if (wavePlayer != null)
                        {
                            wavePlayer.Dispose();
                        }

                        if (audioFile != null)
                        {
                            audioFile.Dispose();
                        }

                        try
                        {
                            if (File.Exists(filePath))
                            {
                                File.Delete(filePath);
                                Console.WriteLine($"Temp audio file deleted: {filePath}");
                            }
                        }
                        catch (Exception ex)
                        {
                            Console.WriteLine($"Failed to delete temp audio file: {ex.Message}");
                        }
                    }
                });
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Error starting audio playback thread: {ex.Message}");
            }
        }
    }
}

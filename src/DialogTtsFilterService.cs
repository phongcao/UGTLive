using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace UGTLive
{
    public sealed class DialogTtsFilterService
    {
        private const string DefaultApiBase = "http://127.0.0.1:1234";
        private const string DefaultModel = "qwen2.5-vl-7b-instruct";
        private const string NoDialogSentinel = "__UGTLIVE_NO_DIALOG__";

        private static DialogTtsFilterService? _instance;
        private readonly HttpClient _httpClient;
        private readonly ConcurrentDictionary<string, string> _cache = new ConcurrentDictionary<string, string>();

        public static DialogTtsFilterService Instance
        {
            get
            {
                if (_instance == null)
                {
                    _instance = new DialogTtsFilterService();
                }

                return _instance;
            }
        }

        private DialogTtsFilterService()
        {
            _httpClient = new HttpClient
            {
                Timeout = TimeSpan.FromSeconds(60)
            };
            _httpClient.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
        }

        public void ClearCache()
        {
            _cache.Clear();
        }

        public async Task<string?> FilterTextAsync(string text, string? languageCode = null, CancellationToken cancellationToken = default)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return null;
            }

            string trimmedText = text.Trim();
            if (!ConfigManager.Instance.IsDialogTtsEnabled())
            {
                return trimmedText;
            }

            string apiBase = ConfigManager.Instance.GetDialogTtsApiBase();
            if (string.IsNullOrWhiteSpace(apiBase))
                apiBase = ConfigManager.Instance.GetGenericLlmOcrApiBase();
            string apiKey = ConfigManager.Instance.GetDialogTtsApiKey();
            if (string.IsNullOrWhiteSpace(apiKey))
                apiKey = ConfigManager.Instance.GetGenericLlmOcrApiKey();
            string model = ConfigManager.Instance.GetDialogTtsModel();
            if (string.IsNullOrWhiteSpace(model))
                model = ConfigManager.Instance.GetGenericLlmOcrModel();
            string cacheKey = string.Join("\n",
                apiBase?.Trim() ?? DefaultApiBase,
                apiKey?.Trim() ?? string.Empty,
                model?.Trim() ?? DefaultModel,
                languageCode?.Trim() ?? string.Empty,
                trimmedText);

            if (_cache.TryGetValue(cacheKey, out string? cachedValue))
            {
                return string.IsNullOrWhiteSpace(cachedValue) ? null : cachedValue;
            }

            try
            {
                string endpoint = BuildEndpoint(apiBase);
                string responseText = await SendFilterRequestAsync(endpoint, apiKey, model, trimmedText, languageCode, cancellationToken);
                string filteredText = NormalizeResponse(responseText);

                if (IsNoDialogResponse(filteredText))
                {
                    _cache[cacheKey] = string.Empty;
                    return null;
                }

                _cache[cacheKey] = filteredText;
                return string.IsNullOrWhiteSpace(filteredText) ? null : filteredText;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"DialogTtsFilterService: Failed to filter text, falling back to original. {ex.Message}");
                return trimmedText;
            }
        }

        private async Task<string> SendFilterRequestAsync(string endpoint, string? apiKey, string? model, string text, string? languageCode, CancellationToken cancellationToken)
        {
            string languageHint = GetLanguageName(languageCode);

            var requestBody = new Dictionary<string, object>
            {
                ["model"] = string.IsNullOrWhiteSpace(model) ? DefaultModel : model.Trim(),
                ["messages"] = new object[]
                {
                    new Dictionary<string, string>
                    {
                        ["role"] = "system",
                        ["content"] =
                            "You clean text before video-game text-to-speech playback. Keep only actual spoken dialogue or narration that should be read aloud. " +
                            "Remove speaker names, name tags, menu labels, HUD text, button prompts, inventory/status text, quest headers, control hints, and standalone character names unless they are part of a spoken sentence. " +
                            "Keep the original language and wording of the remaining spoken text. " +
                            $"If nothing should be spoken, reply with EXACTLY {NoDialogSentinel}. " +
                            "Reply with only the cleaned text and nothing else."
                    },
                    new Dictionary<string, string>
                    {
                        ["role"] = "user",
                        ["content"] = $"Language hint: {languageHint}\n\nText to clean for TTS:\n{text}"
                    }
                },
                ["temperature"] = 0,
                ["max_tokens"] = 512,
                ["chat_template_kwargs"] = new Dictionary<string, bool>
                {
                    ["enable_thinking"] = false
                }
            };

            string requestJson = JsonSerializer.Serialize(requestBody);
            using HttpRequestMessage request = new HttpRequestMessage(HttpMethod.Post, endpoint)
            {
                Content = new StringContent(requestJson, Encoding.UTF8, "application/json")
            };

            if (!string.IsNullOrWhiteSpace(apiKey) && !apiKey.Contains("<your", StringComparison.OrdinalIgnoreCase))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey.Trim());
            }

            using HttpResponseMessage response = await _httpClient.SendAsync(request, cancellationToken);
            string responseContent = await response.Content.ReadAsStringAsync(cancellationToken);

            if (!response.IsSuccessStatusCode)
            {
                throw new HttpRequestException($"Dialog TTS filter request failed: {response.StatusCode}. {responseContent}");
            }

            using JsonDocument doc = JsonDocument.Parse(responseContent);
            if (!doc.RootElement.TryGetProperty("choices", out JsonElement choicesElement) || choicesElement.GetArrayLength() == 0)
            {
                throw new InvalidOperationException("Dialog TTS filter response did not contain any choices.");
            }

            JsonElement messageElement = choicesElement[0].GetProperty("message");
            if (!messageElement.TryGetProperty("content", out JsonElement contentElement))
            {
                throw new InvalidOperationException("Dialog TTS filter response did not contain message content.");
            }

            return ExtractContentText(contentElement);
        }

        private static string ExtractContentText(JsonElement contentElement)
        {
            if (contentElement.ValueKind == JsonValueKind.String)
            {
                return contentElement.GetString() ?? string.Empty;
            }

            if (contentElement.ValueKind != JsonValueKind.Array)
            {
                return string.Empty;
            }

            StringBuilder builder = new StringBuilder();
            foreach (JsonElement item in contentElement.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.String)
                {
                    builder.Append(item.GetString());
                    continue;
                }

                if (item.ValueKind != JsonValueKind.Object)
                {
                    continue;
                }

                if (item.TryGetProperty("text", out JsonElement textElement) && textElement.ValueKind == JsonValueKind.String)
                {
                    builder.Append(textElement.GetString());
                    continue;
                }

                if (item.TryGetProperty("value", out JsonElement valueElement) && valueElement.ValueKind == JsonValueKind.String)
                {
                    builder.Append(valueElement.GetString());
                }
            }

            return builder.ToString();
        }

        private static string NormalizeResponse(string responseText)
        {
            string cleaned = responseText?.Trim() ?? string.Empty;
            cleaned = cleaned.Replace("\r\n", "\n");
            cleaned = Regex.Replace(cleaned, "\\n{3,}", "\n\n");
            cleaned = Regex.Replace(cleaned, "[ \t]+", " ");
            cleaned = Regex.Replace(cleaned, " *\n *", "\n");
            return cleaned.Trim();
        }

        private static bool IsNoDialogResponse(string text)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return true;
            }

            string normalized = text.Trim();
            return normalized.Contains(NoDialogSentinel, StringComparison.OrdinalIgnoreCase)
                || normalized.Equals("no dialog", StringComparison.OrdinalIgnoreCase)
                || normalized.Equals("no spoken dialog", StringComparison.OrdinalIgnoreCase)
                || normalized.Equals("no spoken dialogue", StringComparison.OrdinalIgnoreCase)
                || normalized.Equals("nothing to speak", StringComparison.OrdinalIgnoreCase)
                || normalized.Equals("none", StringComparison.OrdinalIgnoreCase);
        }

        private static string BuildEndpoint(string? apiBase)
        {
            string baseUrl = string.IsNullOrWhiteSpace(apiBase) ? DefaultApiBase : apiBase.Trim();
            baseUrl = baseUrl.TrimEnd('/');

            if (baseUrl.EndsWith("/v1/chat/completions", StringComparison.OrdinalIgnoreCase) ||
                baseUrl.EndsWith("/chat/completions", StringComparison.OrdinalIgnoreCase))
            {
                return baseUrl;
            }

            if (baseUrl.EndsWith("/v1", StringComparison.OrdinalIgnoreCase))
            {
                return baseUrl + "/chat/completions";
            }

            return baseUrl + "/v1/chat/completions";
        }

        private static string GetLanguageName(string? languageCode)
        {
            return (languageCode ?? string.Empty).Trim().ToLowerInvariant() switch
            {
                "ja" => "Japanese",
                "en" => "English",
                "ch_sim" => "Simplified Chinese",
                "ch_tra" => "Traditional Chinese",
                "ko" => "Korean",
                "es" => "Spanish",
                "fr" => "French",
                "de" => "German",
                "it" => "Italian",
                "pt" => "Portuguese",
                "ru" => "Russian",
                "vi" => "Vietnamese",
                "th" => "Thai",
                _ => string.IsNullOrWhiteSpace(languageCode) ? "unknown" : languageCode.Trim()
            };
        }
    }
}
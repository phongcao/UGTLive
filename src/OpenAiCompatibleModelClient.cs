using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Threading.Tasks;

namespace UGTLive
{
    internal static class OpenAiCompatibleModelClient
    {
        private static readonly HttpClient _httpClient = new()
        {
            Timeout = TimeSpan.FromSeconds(30),
        };

        public static string BuildModelsEndpoint(string apiBase)
        {
            string normalizedBase = NormalizeApiBase(apiBase);

            if (normalizedBase.EndsWith("/v1/models", StringComparison.OrdinalIgnoreCase))
            {
                return normalizedBase;
            }

            if (normalizedBase.EndsWith("/v1/chat/completions", StringComparison.OrdinalIgnoreCase))
            {
                return normalizedBase[..^"/chat/completions".Length] + "/models";
            }

            if (normalizedBase.EndsWith("/chat/completions", StringComparison.OrdinalIgnoreCase))
            {
                return normalizedBase[..^"/chat/completions".Length] + "/models";
            }

            if (normalizedBase.EndsWith("/v1", StringComparison.OrdinalIgnoreCase))
            {
                return normalizedBase + "/models";
            }

            return normalizedBase + "/v1/models";
        }

        public static async Task<List<string>> FetchModelsAsync(string apiBase, string apiKey = "")
        {
            string endpoint = BuildModelsEndpoint(apiBase);

            using HttpRequestMessage request = new(HttpMethod.Get, endpoint);
            if (!string.IsNullOrWhiteSpace(apiKey) && !apiKey.StartsWith("<your", StringComparison.OrdinalIgnoreCase))
            {
                request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", apiKey.Trim());
            }

            using HttpResponseMessage response = await _httpClient.SendAsync(request);
            string jsonResponse = await response.Content.ReadAsStringAsync();

            if (!response.IsSuccessStatusCode)
            {
                throw new HttpRequestException(
                    $"Model list request failed: {(int)response.StatusCode} {response.ReasonPhrase}. {jsonResponse}".Trim());
            }

            return ParseModelIds(jsonResponse);
        }

        private static string NormalizeApiBase(string apiBase)
        {
            string normalizedBase = (apiBase ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(normalizedBase))
            {
                normalizedBase = "http://127.0.0.1:1234";
            }

            if (!normalizedBase.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                && !normalizedBase.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            {
                normalizedBase = "http://" + normalizedBase;
            }

            return normalizedBase.TrimEnd('/');
        }

        private static List<string> ParseModelIds(string jsonResponse)
        {
            using JsonDocument doc = JsonDocument.Parse(jsonResponse);
            HashSet<string> models = new(StringComparer.OrdinalIgnoreCase);

            if (doc.RootElement.ValueKind == JsonValueKind.Object)
            {
                if (doc.RootElement.TryGetProperty("data", out JsonElement dataElement)
                    && dataElement.ValueKind == JsonValueKind.Array)
                {
                    AddModelIds(models, dataElement);
                }

                if (doc.RootElement.TryGetProperty("models", out JsonElement modelsElement)
                    && modelsElement.ValueKind == JsonValueKind.Array)
                {
                    AddModelIds(models, modelsElement);
                }
            }
            else if (doc.RootElement.ValueKind == JsonValueKind.Array)
            {
                AddModelIds(models, doc.RootElement);
            }

            return models.OrderBy(model => model, StringComparer.OrdinalIgnoreCase).ToList();
        }

        private static void AddModelIds(ISet<string> models, JsonElement element)
        {
            foreach (JsonElement item in element.EnumerateArray())
            {
                if (item.ValueKind == JsonValueKind.String)
                {
                    string? modelName = item.GetString();
                    if (!string.IsNullOrWhiteSpace(modelName))
                    {
                        models.Add(modelName.Trim());
                    }

                    continue;
                }

                if (item.ValueKind != JsonValueKind.Object)
                {
                    continue;
                }

                if (item.TryGetProperty("id", out JsonElement idElement))
                {
                    string? modelId = idElement.GetString();
                    if (!string.IsNullOrWhiteSpace(modelId))
                    {
                        models.Add(modelId.Trim());
                    }
                }
            }
        }
    }
}
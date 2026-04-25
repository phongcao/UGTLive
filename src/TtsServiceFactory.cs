using System;

namespace UGTLive
{
    public static class TtsServiceFactory
    {
        public static ITtsService CreateService()
        {
            string currentService = ConfigManager.Instance.GetTtsService();
            return CreateService(currentService);
        }

        public static ITtsService CreateService(string serviceName)
        {
            ITtsService service = serviceName switch
            {
                "Google Cloud TTS" => GoogleTTSService.Instance,
                "Qwen3-TTS" => Qwen3TtsService.Instance,
                "VieNeu-GGUF-TTS" => VieNeuGgufTtsService.Instance,
                _ => ElevenLabsService.Instance
            };

            return new TimedTtsService(service);
        }

        public static bool IsLocalService(string serviceName)
        {
            if (serviceName == "VieNeu-GGUF-TTS")
                return true;
            return serviceName == "Qwen3-TTS" && ConfigManager.Instance.IsQwen3TtsLocalBackend();
        }
    }
}

using System.Diagnostics;
using System.Threading.Tasks;

namespace UGTLive
{
    public class TimedTtsService : ITtsService
    {
        private readonly ITtsService _innerService;

        public TimedTtsService(ITtsService innerService)
        {
            _innerService = innerService;
        }

        public async Task<bool> SpeakText(string text)
        {
            if (!ConfigManager.Instance.IsTtsEnabled())
            {
                Console.WriteLine("TimedTtsService: TTS request ignored because TTS is disabled");
                return false;
            }

            Stopwatch stopwatch = Stopwatch.StartNew();
            bool success = await _innerService.SpeakText(text);
            stopwatch.Stop();

            if (success)
            {
                TranslationStatus.SetLastTtsProcessingTime(stopwatch.ElapsedMilliseconds);
                Logic.Instance.RefreshOCRStatusDisplay();
            }

            return success;
        }

        public async Task<string?> GenerateAudioFileAsync(string text, string voiceId)
        {
            if (!ConfigManager.Instance.IsTtsEnabled())
            {
                Console.WriteLine("TimedTtsService: Audio generation ignored because TTS is disabled");
                return null;
            }

            Stopwatch stopwatch = Stopwatch.StartNew();
            string? audioFilePath = await _innerService.GenerateAudioFileAsync(text, voiceId);
            stopwatch.Stop();

            if (!string.IsNullOrEmpty(audioFilePath))
            {
                TranslationStatus.SetLastTtsProcessingTime(stopwatch.ElapsedMilliseconds);
                Logic.Instance.RefreshOCRStatusDisplay();
            }

            return audioFilePath;
        }
    }
}
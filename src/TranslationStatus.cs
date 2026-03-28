using System;

namespace UGTLive
{
    /// <summary>
    /// Static class to track translation status for UI updates.
    /// Updated by translation services, read by UI timers.
    /// Also broadcasts status messages to all windows via StatusChanged event.
    /// </summary>
    public static class TranslationStatus
    {
        private static readonly object _lock = new object();
        
        // Current token count being received
        private static int _tokenCount = 0;
        
        // Whether the model is currently in "thinking" mode
        private static bool _isThinking = false;
        
        // Whether streaming is active
        private static bool _isStreaming = false;
        
        // Current status message displayed across all windows
        private static string _currentMessage = "Stopped";

        // Last completed end-to-end OCR + translation processing time in milliseconds
        private static long? _lastOcrTranslateProcessingTimeMs = null;

        // Last completed TTS processing time in milliseconds
        private static long? _lastTtsProcessingTimeMs = null;
        
        /// <summary>
        /// Event raised when status message changes
        /// </summary>
        public static event EventHandler<string>? StatusChanged;
        
        /// <summary>
        /// Current token count received during streaming
        /// </summary>
        public static int TokenCount
        {
            get { lock (_lock) { return _tokenCount; } }
            set { lock (_lock) { _tokenCount = value; } }
        }
        
        /// <summary>
        /// Whether the model is currently outputting thinking/reasoning content
        /// </summary>
        public static bool IsThinking
        {
            get { lock (_lock) { return _isThinking; } }
            set { lock (_lock) { _isThinking = value; } }
        }
        
        /// <summary>
        /// Whether streaming is currently active
        /// </summary>
        public static bool IsStreaming
        {
            get { lock (_lock) { return _isStreaming; } }
            set { lock (_lock) { _isStreaming = value; } }
        }
        
        /// <summary>
        /// Current status message displayed across all windows
        /// </summary>
        public static string CurrentMessage
        {
            get { lock (_lock) { return _currentMessage; } }
        }

        /// <summary>
        /// Last completed OCR + translation processing time in milliseconds.
        /// </summary>
        public static long? LastOcrTranslateProcessingTimeMs
        {
            get { lock (_lock) { return _lastOcrTranslateProcessingTimeMs; } }
        }

        /// <summary>
        /// Last completed TTS processing time in milliseconds.
        /// </summary>
        public static long? LastTtsProcessingTimeMs
        {
            get { lock (_lock) { return _lastTtsProcessingTimeMs; } }
        }
        
        /// <summary>
        /// Set the status message and notify all subscribers
        /// </summary>
        public static void SetStatus(string message)
        {
            bool changed = false;
            lock (_lock)
            {
                if (_currentMessage != message)
                {
                    _currentMessage = message;
                    changed = true;
                }
            }
            
            if (changed)
            {
                StatusChanged?.Invoke(null, message);
            }
        }
        
        /// <summary>
        /// Reset all status values (call when starting a new translation)
        /// </summary>
        public static void Reset()
        {
            lock (_lock)
            {
                _tokenCount = 0;
                _isThinking = false;
                _isStreaming = false;
            }
        }

        /// <summary>
        /// Clear the last OCR + translation processing time.
        /// </summary>
        public static void ClearLastOcrTranslateProcessingTime()
        {
            lock (_lock)
            {
                _lastOcrTranslateProcessingTimeMs = null;
            }
        }

        /// <summary>
        /// Record the last completed OCR + translation processing time.
        /// </summary>
        public static void SetLastOcrTranslateProcessingTime(long elapsedMilliseconds)
        {
            lock (_lock)
            {
                _lastOcrTranslateProcessingTimeMs = elapsedMilliseconds;
            }
        }

        /// <summary>
        /// Record the last completed TTS processing time.
        /// </summary>
        public static void SetLastTtsProcessingTime(long elapsedMilliseconds)
        {
            lock (_lock)
            {
                _lastTtsProcessingTimeMs = elapsedMilliseconds;
            }
        }

        /// <summary>
        /// Build the shared OCR status message including the latest processing metrics.
        /// </summary>
        public static string BuildOcrStatusMessage(string ocrMethod, double fps)
        {
            lock (_lock)
            {
                string ocrTranslateTime = FormatElapsedMilliseconds(_lastOcrTranslateProcessingTimeMs);
                string ttsTime = FormatElapsedMilliseconds(_lastTtsProcessingTimeMs);
                return $"{ocrMethod} (fps: {fps:F1}, ocr+tr: {ocrTranslateTime}, tts: {ttsTime})";
            }
        }

        private static string FormatElapsedMilliseconds(long? elapsedMilliseconds)
        {
            if (!elapsedMilliseconds.HasValue)
            {
                return "--";
            }

            if (elapsedMilliseconds.Value >= 10000)
            {
                return $"{elapsedMilliseconds.Value / 1000.0:F1}s";
            }

            return $"{elapsedMilliseconds.Value} ms";
        }
        
        /// <summary>
        /// Start streaming mode
        /// </summary>
        public static void StartStreaming(bool isThinkingModel = false)
        {
            lock (_lock)
            {
                _tokenCount = 0;
                _isThinking = isThinkingModel;
                _isStreaming = true;
            }
        }
        
        /// <summary>
        /// Stop streaming mode
        /// </summary>
        public static void StopStreaming()
        {
            lock (_lock)
            {
                _isStreaming = false;
                _isThinking = false;
            }
        }
        
        /// <summary>
        /// Increment token count (thread-safe)
        /// </summary>
        public static void IncrementTokenCount(int count = 1)
        {
            lock (_lock)
            {
                _tokenCount += count;
            }
        }
    }
}


using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using WpfColor = System.Windows.Media.Color;

namespace UGTLive
{
    public partial class ToolbarWindow : Window
    {
        [DllImport("user32.dll")]
        private static extern bool SetWindowDisplayAffinity(IntPtr hwnd, uint dwAffinity);

        [DllImport("user32.dll")]
        private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

        private static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
        private const uint SWP_NOMOVE = 0x0002;
        private const uint SWP_NOSIZE = 0x0001;
        private const uint SWP_NOACTIVATE = 0x0010;

        private const uint WDA_NONE = 0x00000000;
        private const uint WDA_EXCLUDEFROMCAPTURE = 0x00000011;

        // For enumerating windows to populate the target window dropdown
        [DllImport("user32.dll")]
        private static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
        
        [DllImport("user32.dll", CharSet = CharSet.Auto)]
        private static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
        
        [DllImport("user32.dll")]
        private static extern bool IsWindowVisible(IntPtr hWnd);
        
        [DllImport("user32.dll")]
        private static extern int GetWindowTextLength(IntPtr hWnd);

        [DllImport("user32.dll")]
        private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint lpdwProcessId);
        
        private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

        private const string DEFAULT_CAPTURE_ITEM = "Screen capture (default)";

        // Language options for toolbar ComboBoxes (display name → config code)
        private static readonly (string Display, string Code)[] SOURCE_LANG_OPTIONS = new[]
        {
            ("Chinese", "zh"),
            ("Japanese", "ja"),
            ("English", "en"),
        };

        private static readonly (string Display, string Code)[] TARGET_LANG_OPTIONS = new[]
        {
            ("English", "en"),
            ("Vietnamese", "vi"),
        };

        private const string GENERIC_LLM_MODELS_LOADING_ITEM = "Loading models...";

        public static ToolbarWindow? Instance { get; private set; }

        private bool _isInitialized = false;
        private bool _hasLoadedGenericLlmModels = false;
        private bool _isGenericLlmModelsLoading = false;
        private bool _suppressGenericLlmModelSelectionChanged = false;
        private string _lastGenericLlmModelApiBase = string.Empty;
        private string _lastGenericLlmModelApiKey = string.Empty;

        public ToolbarWindow()
        {
            if (Instance != null && Instance != this && Instance.IsLoaded)
            {
                try
                {
                    Instance.Close();
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error closing previous toolbar instance: {ex.Message}");
                }
            }

            Instance = this;
            InitializeComponent();
            IconHelper.SetWindowIcon(this);

            this.SourceInitialized += ToolbarWindow_SourceInitialized;
            this.Loaded += ToolbarWindow_Loaded;
        }

        private void ToolbarWindow_SourceInitialized(object? sender, EventArgs e)
        {
            // WDA_EXCLUDEFROMCAPTURE (0x11) makes the toolbar completely invisible on some
            // systems/GPU drivers. Disabled until a reliable alternative is found.
            // SetExcludeFromCapture();
        }

        private async void ToolbarWindow_Loaded(object sender, RoutedEventArgs e)
        {
            SyncTargetWindow();
            SyncGenericLlmModel(ConfigManager.Instance.GetGenericLlmOcrModel());
            _isInitialized = true;
            await RefreshCacheStatsAsync();
        }

        // --- Capture exclusion ---

        private void SetExcludeFromCapture()
        {
            try
            {
                bool visibleInScreenshots = ConfigManager.Instance.GetWindowsVisibleInScreenshots();
                var helper = new WindowInteropHelper(this);
                IntPtr hwnd = helper.Handle;

                if (hwnd != IntPtr.Zero)
                {
                    uint affinity = visibleInScreenshots ? WDA_NONE : WDA_EXCLUDEFROMCAPTURE;
                    bool success = SetWindowDisplayAffinity(hwnd, affinity);
                    Console.WriteLine($"Toolbar window {(visibleInScreenshots ? "included in" : "excluded from")} screen capture (HWND: {hwnd}, success: {success})");
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Error setting toolbar capture exclusion: {ex.Message}");
            }
        }

        public void UpdateCaptureExclusion()
        {
            SetExcludeFromCapture();
        }

        public void BringToFront()
        {
            var helper = new WindowInteropHelper(this);
            IntPtr hwnd = helper.Handle;
            if (hwnd != IntPtr.Zero)
            {
                SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            }
        }

        protected override void OnDeactivated(EventArgs e)
        {
            base.OnDeactivated(e);

            if (SettingsWindow.IsOpenAndVisible())
            {
                return;
            }

            Dispatcher.BeginInvoke(new Action(() =>
            {
                if (IsVisible && !SettingsWindow.IsOpenAndVisible())
                {
                    BringToFront();
                }
            }), DispatcherPriority.Input);
        }

        protected override void OnClosed(EventArgs e)
        {
            base.OnClosed(e);

            if (Instance == this)
            {
                Instance = null;
            }
        }

        // --- Window-level drag (anywhere that isn't an interactive control) ---

        private bool isInteractiveElement(DependencyObject? element)
        {
            while (element != null)
            {
                if (element is System.Windows.Controls.Button ||
                    element is System.Windows.Controls.CheckBox ||
                    element is System.Windows.Controls.RadioButton ||
                    element is System.Windows.Controls.ComboBox)
                    return true;
                element = System.Windows.Media.VisualTreeHelper.GetParent(element);
            }
            return false;
        }

        private void Window_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (isInteractiveElement(e.OriginalSource as DependencyObject))
                return;

            if (e.ClickCount == 2)
            {
                MainWindow.Instance?.ResetToolbarPosition();
                e.Handled = true;
                return;
            }

            this.DragMove();
            e.Handled = true;
            MainWindow.Instance?.SaveToolbarOffset();
        }

        // --- Button click delegates (forward to MainWindow) ---

        private void HideButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleHideButton(userInitiated: true);
        }

        private void DrawBorderButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleDrawBorderButton();
        }

        private void ResetBorderButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleResetBorderButton();
        }

        // --- Capture Region Preset Buttons ---
        
        private int _selectedRegionSlot = 0; // Which slot Save/Del will target (0-5)
        
        private void RegionButton_Click(object sender, RoutedEventArgs e)
        {
            if (sender is System.Windows.Controls.Button btn && btn.Tag is string tagStr && int.TryParse(tagStr, out int index))
            {
                _selectedRegionSlot = index;
                
                // If this region has a saved position, switch to it
                if (ConfigManager.Instance.HasCaptureRegion(index))
                {
                    MainWindow.Instance?.SwitchToCaptureRegion(index);
                }
                else
                {
                    // Just select the slot for saving
                    UpdateCaptureRegionButtons(MainWindow.Instance?.GetActiveCaptureRegionIndex() ?? -1);
                }
            }
        }
        
        private void RegionSaveButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.SaveCaptureRegion(_selectedRegionSlot);
        }
        
        private void RegionDeleteButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.DeleteCaptureRegion(_selectedRegionSlot);
        }
        
        // Update the visual state of region buttons
        public void UpdateCaptureRegionButtons(int activeIndex, bool syncSelectedSlot = false)
        {
            if (syncSelectedSlot && activeIndex >= 0 && activeIndex <= 5)
            {
                _selectedRegionSlot = activeIndex;
            }

            var buttons = new[] { regionBtn0, regionBtn1, regionBtn2, regionBtn3, regionBtn4, regionBtn5 };
            
            for (int i = 0; i < buttons.Length; i++)
            {
                int regionIndex = i;
                bool hasSaved = ConfigManager.Instance.HasCaptureRegion(regionIndex);
                bool isActive = (regionIndex == activeIndex);
                bool isSelected = (regionIndex == _selectedRegionSlot);
                
                if (isActive)
                {
                    // Active region: green background
                    buttons[i].Background = new SolidColorBrush(WpfColor.FromRgb(46, 160, 67));
                    buttons[i].BorderBrush = new SolidColorBrush(WpfColor.FromRgb(80, 220, 100));
                }
                else if (hasSaved)
                {
                    // Saved but not active: white border to indicate it has data
                    buttons[i].Background = new SolidColorBrush(WpfColor.FromRgb(70, 70, 90));
                    buttons[i].BorderBrush = new SolidColorBrush(Colors.White);
                }
                else
                {
                    // Empty slot: dim appearance
                    buttons[i].Background = new SolidColorBrush(WpfColor.FromRgb(95, 95, 95));
                    buttons[i].BorderBrush = new SolidColorBrush(WpfColor.FromRgb(128, 128, 128));
                }
                
                // Add underline to selected slot (the one Save/Del will target)
                if (isSelected && !isActive)
                {
                    buttons[i].BorderBrush = new SolidColorBrush(WpfColor.FromRgb(255, 200, 50));
                }
            }
        }

        private void MinimizeAppButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleMinimizeButton();
        }

        private void CloseAppButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.Close();
        }

        private void ToggleButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleToggleButton();
        }

        private void SnapshotButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleSnapshotButton();
        }

        private void MonitorButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleMonitorButton();
        }

        private void ChatBoxButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleChatBoxButton();
        }

        private void ListenButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleListenButton();
        }

        private void LogButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleLogButton();
        }

        private void SettingsButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleSettingsButton();
        }

        private void ExportButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandleExportButton();
        }

        private void PlayAllAudioButton_Click(object sender, RoutedEventArgs e)
        {
            MainWindow.Instance?.HandlePlayAllAudioButton();
        }

        private void OverlayRadioButton_Checked(object sender, RoutedEventArgs e)
        {
            if (!_isInitialized) return;
            MainWindow.Instance?.HandleOverlayRadioChanged(sender);
        }

        private void MousePassthroughCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            if (!_isInitialized) return;
            MainWindow.Instance?.HandlePassthroughChanged(mousePassthroughCheckBox.IsChecked ?? false);
        }

        private void TtsEnabledCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            if (!_isInitialized) return;
            MainWindow.Instance?.HandleTtsEnabledChanged(ttsEnabledCheckBox.IsChecked ?? false);
        }

        private void GenericLlmIgnoreMenusCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            if (!_isInitialized) return;
            MainWindow.Instance?.HandleGenericLlmOcrIgnoreMenusChanged(genericLlmIgnoreMenusCheckBox.IsChecked ?? false);
        }

        private void GenericLlmMenuItemsFilterCheckBox_Changed(object sender, RoutedEventArgs e)
        {
            if (!_isInitialized) return;
            MainWindow.Instance?.HandleGenericLlmOcrMenuItemsFilterChanged(genericLlmMenuItemsFilterCheckBox.IsChecked ?? false);
        }

        // --- Target window capture dropdown ---

        /// <summary>
        /// Get a list of visible top-level window titles, excluding our own windows.
        /// </summary>
        private List<string> GetVisibleWindowTitles()
        {
            var titles = new List<string>();
            uint ownPid = 0;
            try
            {
                ownPid = (uint)System.Diagnostics.Process.GetCurrentProcess().Id;
            }
            catch { }

            EnumWindows((hWnd, lParam) =>
            {
                if (!IsWindowVisible(hWnd)) return true;
                int len = GetWindowTextLength(hWnd);
                if (len == 0) return true;
                
                // Skip our own process windows
                if (ownPid != 0)
                {
                    GetWindowThreadProcessId(hWnd, out uint windowPid);
                    if (windowPid == ownPid) return true;
                }

                StringBuilder sb = new StringBuilder(len + 1);
                GetWindowText(hWnd, sb, sb.Capacity);
                string title = sb.ToString();
                if (!string.IsNullOrWhiteSpace(title))
                {
                    titles.Add(title);
                }
                return true;
            }, IntPtr.Zero);

            titles.Sort(StringComparer.OrdinalIgnoreCase);
            return titles;
        }

        private void TargetWindowComboBox_DropDownOpened(object sender, EventArgs e)
        {
            // Remember current selection
            string currentConfig = ConfigManager.Instance.GetTargetWindowTitle();
            
            _isInitialized = false;
            targetWindowComboBox.Items.Clear();
            targetWindowComboBox.Items.Add(DEFAULT_CAPTURE_ITEM);

            var titles = GetVisibleWindowTitles();
            foreach (var title in titles)
            {
                targetWindowComboBox.Items.Add(title);
            }

            // Restore selection
            if (string.IsNullOrEmpty(currentConfig))
            {
                targetWindowComboBox.SelectedIndex = 0;
            }
            else
            {
                bool found = false;
                for (int i = 1; i < targetWindowComboBox.Items.Count; i++)
                {
                    string item = targetWindowComboBox.Items[i] as string ?? "";
                    if (item.IndexOf(currentConfig, StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        targetWindowComboBox.SelectedIndex = i;
                        found = true;
                        break;
                    }
                }
                if (!found)
                {
                    // The previously configured window is no longer visible; keep default
                    targetWindowComboBox.SelectedIndex = 0;
                }
            }
            _isInitialized = true;
        }

        private void TargetWindowComboBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            if (!_isInitialized) return;
            
            string selected = targetWindowComboBox.SelectedItem as string ?? "";
            if (selected == DEFAULT_CAPTURE_ITEM || string.IsNullOrEmpty(selected))
            {
                ConfigManager.Instance.SetTargetWindowTitle("");
            }
            else
            {
                ConfigManager.Instance.SetTargetWindowTitle(selected);
            }
        }

        private void SourceLangComboBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            if (!_isInitialized) return;
            int idx = sourceLangComboBox.SelectedIndex;
            if (idx >= 0 && idx < SOURCE_LANG_OPTIONS.Length)
            {
                MainWindow.Instance?.HandleSourceLanguageChanged(SOURCE_LANG_OPTIONS[idx].Code);
            }
        }

        private void TargetLangComboBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            if (!_isInitialized) return;
            int idx = targetLangComboBox.SelectedIndex;
            if (idx >= 0 && idx < TARGET_LANG_OPTIONS.Length)
            {
                MainWindow.Instance?.HandleTargetLanguageChanged(TARGET_LANG_OPTIONS[idx].Code);
            }
        }

        private async void GenericLlmModelComboBox_DropDownOpened(object sender, EventArgs e)
        {
            await RefreshGenericLlmModelsAsync();
        }

        private void GenericLlmModelComboBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
        {
            if (!_isInitialized || _suppressGenericLlmModelSelectionChanged)
            {
                return;
            }

            string selectedModel = (genericLlmModelComboBox.SelectedItem as string ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(selectedModel))
            {
                return;
            }

            MainWindow.Instance?.HandleGenericLlmOcrModelChanged(selectedModel);
        }

        private async Task RefreshGenericLlmModelsAsync()
        {
            if (_isGenericLlmModelsLoading)
            {
                return;
            }

            string apiBase = ConfigManager.Instance.GetGenericLlmOcrApiBase();
            string apiKey = ConfigManager.Instance.GetGenericLlmOcrApiKey();
            bool configChanged = !string.Equals(apiBase, _lastGenericLlmModelApiBase, StringComparison.Ordinal)
                || !string.Equals(apiKey, _lastGenericLlmModelApiKey, StringComparison.Ordinal);

            if (_hasLoadedGenericLlmModels && !configChanged)
            {
                return;
            }

            string selectedModel = (genericLlmModelComboBox.SelectedItem as string ?? ConfigManager.Instance.GetGenericLlmOcrModel()).Trim();

            _isGenericLlmModelsLoading = true;
            _suppressGenericLlmModelSelectionChanged = true;
            genericLlmModelComboBox.IsEnabled = false;
            genericLlmModelComboBox.Items.Clear();
            genericLlmModelComboBox.Items.Add(GENERIC_LLM_MODELS_LOADING_ITEM);
            genericLlmModelComboBox.SelectedIndex = 0;

            try
            {
                List<string> models = await OpenAiCompatibleModelClient.FetchModelsAsync(apiBase, apiKey);
                _lastGenericLlmModelApiBase = apiBase;
                _lastGenericLlmModelApiKey = apiKey;
                _hasLoadedGenericLlmModels = true;
                SetGenericLlmModelItems(models, selectedModel);
            }
            catch (Exception ex)
            {
                _hasLoadedGenericLlmModels = false;
                SetGenericLlmModelItems(Array.Empty<string>(), selectedModel);
                Console.WriteLine($"Error fetching Generic LLM OCR models: {ex.Message}");
            }
            finally
            {
                genericLlmModelComboBox.IsEnabled = true;
                _suppressGenericLlmModelSelectionChanged = false;
                genericLlmModelComboBox.ToolTip = genericLlmModelComboBox.SelectedItem as string
                    ?? "Generic LLM OCR model from the OpenAI-compatible /v1/models endpoint";
                _isGenericLlmModelsLoading = false;
            }
        }

        private void SetGenericLlmModelItems(IEnumerable<string> models, string selectedModel)
        {
            string normalizedSelectedModel = (selectedModel ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(normalizedSelectedModel))
            {
                normalizedSelectedModel = ConfigManager.Instance.GetGenericLlmOcrModel();
            }

            List<string> items = models
                .Where(model => !string.IsNullOrWhiteSpace(model))
                .Select(model => model.Trim())
                .Where(model => !string.Equals(model, GENERIC_LLM_MODELS_LOADING_ITEM, StringComparison.OrdinalIgnoreCase))
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .OrderBy(model => model, StringComparer.OrdinalIgnoreCase)
                .ToList();

            if (!string.IsNullOrWhiteSpace(normalizedSelectedModel)
                && !items.Contains(normalizedSelectedModel, StringComparer.OrdinalIgnoreCase))
            {
                items.Insert(0, normalizedSelectedModel);
            }

            if (items.Count == 0)
            {
                items.Add(ConfigManager.Instance.GetGenericLlmOcrModel());
            }

            genericLlmModelComboBox.Items.Clear();
            foreach (string item in items)
            {
                genericLlmModelComboBox.Items.Add(item);
            }

            string selectedItem = items.FirstOrDefault(item => string.Equals(item, normalizedSelectedModel, StringComparison.OrdinalIgnoreCase))
                ?? items[0];
            genericLlmModelComboBox.SelectedItem = selectedItem;
            genericLlmModelComboBox.ToolTip = selectedItem;
        }

        /// <summary>
        /// Initialize the target window combo box from saved config.
        /// </summary>
        public void SyncTargetWindow()
        {
            _isInitialized = false;
            targetWindowComboBox.Items.Clear();
            targetWindowComboBox.Items.Add(DEFAULT_CAPTURE_ITEM);

            string configTitle = ConfigManager.Instance.GetTargetWindowTitle();
            if (string.IsNullOrEmpty(configTitle))
            {
                targetWindowComboBox.SelectedIndex = 0;
            }
            else
            {
                targetWindowComboBox.Items.Add(configTitle);
                targetWindowComboBox.SelectedIndex = 1;
            }
            _isInitialized = true;
        }

        // --- Sync state from MainWindow ---

        public void SyncOverlayMode(string mode)
        {
            _isInitialized = false;
            switch (mode)
            {
                case "Hide":
                    overlayHideRadio.IsChecked = true;
                    break;
                case "Source":
                    overlaySourceRadio.IsChecked = true;
                    break;
                case "Translated":
                    overlayTranslatedRadio.IsChecked = true;
                    break;
            }
            _isInitialized = true;
        }

        public void SyncPassthrough(bool enabled)
        {
            _isInitialized = false;
            mousePassthroughCheckBox.IsChecked = enabled;
            _isInitialized = true;
        }

        public void SyncTtsEnabled(bool enabled)
        {
            _isInitialized = false;
            ttsEnabledCheckBox.IsChecked = enabled;
            _isInitialized = true;
        }

        public void SyncGenericLlmIgnoreMenus(bool enabled)
        {
            _isInitialized = false;
            genericLlmIgnoreMenusCheckBox.IsChecked = enabled;
            _isInitialized = true;
        }

        public void SyncGenericLlmMenuItemsFilter(bool enabled)
        {
            _isInitialized = false;
            genericLlmMenuItemsFilterCheckBox.IsChecked = enabled;
            _isInitialized = true;
        }

        public void SyncSourceLanguage(string langCode)
        {
            _isInitialized = false;
            sourceLangComboBox.Items.Clear();
            int selectedIndex = 0;
            for (int i = 0; i < SOURCE_LANG_OPTIONS.Length; i++)
            {
                sourceLangComboBox.Items.Add(SOURCE_LANG_OPTIONS[i].Display);
                if (string.Equals(SOURCE_LANG_OPTIONS[i].Code, langCode, StringComparison.OrdinalIgnoreCase))
                    selectedIndex = i;
            }
            sourceLangComboBox.SelectedIndex = selectedIndex;
            _isInitialized = true;
        }

        public void SyncTargetLanguage(string langCode)
        {
            _isInitialized = false;
            targetLangComboBox.Items.Clear();
            int selectedIndex = 0;
            for (int i = 0; i < TARGET_LANG_OPTIONS.Length; i++)
            {
                targetLangComboBox.Items.Add(TARGET_LANG_OPTIONS[i].Display);
                if (string.Equals(TARGET_LANG_OPTIONS[i].Code, langCode, StringComparison.OrdinalIgnoreCase))
                    selectedIndex = i;
            }
            targetLangComboBox.SelectedIndex = selectedIndex;
            _isInitialized = true;
        }

        public void SyncGenericLlmModel(string model)
        {
            _suppressGenericLlmModelSelectionChanged = true;
            try
            {
                List<string> currentItems = genericLlmModelComboBox.Items.OfType<string>().ToList();
                SetGenericLlmModelItems(currentItems, model);
            }
            finally
            {
                _suppressGenericLlmModelSelectionChanged = false;
            }
        }

        // --- Translation cache purge ---

        private static readonly System.Net.Http.HttpClient _cacheHttpClient = new System.Net.Http.HttpClient()
        {
            Timeout = TimeSpan.FromSeconds(10)
        };

        private async void PurgeCacheButton_Click(object sender, RoutedEventArgs e)
        {
            purgeCacheButton.IsEnabled = false;
            cacheCountLabel.Text = "Purging...";
            try
            {
                var service = PythonServicesManager.Instance.GetServiceByName("Generic LLM OCR");
                if (service == null || !service.IsRunning)
                {
                    cacheCountLabel.Text = "Service not running";
                    return;
                }

                string baseUrl = $"{service.ServerUrl}:{service.Port}";
                var response = await _cacheHttpClient.PostAsync($"{baseUrl}/translation_cache_purge", null);
                response.EnsureSuccessStatusCode();
                string json = await response.Content.ReadAsStringAsync();
                var doc = System.Text.Json.JsonDocument.Parse(json);
                var root = doc.RootElement;

                int removed = root.GetProperty("removed").GetInt32();
                int totalAfter = root.GetProperty("total_after").GetInt32();
                cacheCountLabel.Text = $"Removed {removed}, now {totalAfter}";
                Console.WriteLine($"Translation cache purge: removed {removed}, remaining {totalAfter}");
            }
            catch (Exception ex)
            {
                cacheCountLabel.Text = "Error";
                Console.WriteLine($"Cache purge failed: {ex.Message}");
            }
            finally
            {
                purgeCacheButton.IsEnabled = true;
            }
        }

        public async Task RefreshCacheStatsAsync()
        {
            try
            {
                var service = PythonServicesManager.Instance.GetServiceByName("Generic LLM OCR");
                if (service == null || !service.IsRunning)
                {
                    cacheCountLabel.Text = "";
                    return;
                }

                string baseUrl = $"{service.ServerUrl}:{service.Port}";
                var response = await _cacheHttpClient.GetAsync($"{baseUrl}/translation_cache_stats");
                response.EnsureSuccessStatusCode();
                string json = await response.Content.ReadAsStringAsync();
                var doc = System.Text.Json.JsonDocument.Parse(json);
                var root = doc.RootElement;

                int total = root.GetProperty("total").GetInt32();
                int wouldRemain = root.GetProperty("would_remain").GetInt32();
                cacheCountLabel.Text = $"{total} items → {wouldRemain}";
            }
            catch
            {
                cacheCountLabel.Text = "";
            }
        }
    }
}

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Windows;
using System.Windows.Input;

namespace UGTLive
{
    // Manages all hotkeys for the application
    public class HotkeyManager
    {
        private static HotkeyManager? _instance;
        private const string HOTKEYS_FILE = "hotkeys.txt";
        
        private Dictionary<string, List<HotkeyEntry>> _actionBindings = new Dictionary<string, List<HotkeyEntry>>();
        private bool _globalHotkeysEnabled = true;
        private bool _isEnabled = true;
        
        // Debouncing for actions to prevent double-triggers
        private Dictionary<string, DateTime> _lastActionTime = new Dictionary<string, DateTime>();
        private const int DEBOUNCE_MS = 100; // 100ms debounce window (prevents double-triggers from gamepad while staying responsive)
        
        public static HotkeyManager Instance
        {
            get
            {
                if (_instance == null)
                {
                    _instance = new HotkeyManager();
                }
                return _instance;
            }
        }
        
        // Events for each action
        public event EventHandler? StartStopRequested;
        public event EventHandler? MonitorToggleRequested;
        public event EventHandler? ChatBoxToggleRequested;
        public event EventHandler? SettingsToggleRequested;
        public event EventHandler? LogToggleRequested;
        public event EventHandler? MainWindowVisibilityToggleRequested;
        public event EventHandler? ClearOverlaysRequested;
        public event EventHandler? PassthroughToggleRequested;
        public event EventHandler? OverlayModeToggleRequested;
        public event EventHandler? OverlayModePreviousRequested;
        public event EventHandler? ListenToggleRequested;
        public event EventHandler? ViewInBrowserRequested;
        public event EventHandler? PlayAllAudioRequested;
        public event EventHandler? SnapshotRequested;
        public event EventHandler<int>? CaptureRegionRequested;  // int = region index 0-5
        
        private HotkeyManager()
        {
            LoadHotkeys();
            
            // Subscribe to gamepad events
            GamepadManager.Instance.ButtonsPressed += GamepadManager_ButtonsPressed;
        }
        
        // Get/set global hotkeys enabled
        public bool GetGlobalHotkeysEnabled()
        {
            return _globalHotkeysEnabled;
        }
        
        public void SetGlobalHotkeysEnabled(bool enabled)
        {
            _globalHotkeysEnabled = enabled;
            Console.WriteLine($"Global hotkeys {(enabled ? "enabled" : "disabled")}");
        }
        
        // Get/set hotkey system enabled
        public bool IsEnabled()
        {
            return _isEnabled;
        }
        
        public void SetEnabled(bool enabled)
        {
            _isEnabled = enabled;
            if (ConfigManager.Instance.GetLogExtraDebugStuff())
            {
                Console.WriteLine($"Hotkey system {(enabled ? "enabled" : "disabled")}");
            }
        }
        
        // Get all action IDs
        public List<string> GetActionIds()
        {
            return new List<string>(_actionBindings.Keys);
        }
        
        // Get all bindings for a specific action
        public List<HotkeyEntry> GetBindings(string actionId)
        {
            if (_actionBindings.TryGetValue(actionId, out var bindings))
            {
                return new List<HotkeyEntry>(bindings);
            }
            return new List<HotkeyEntry>();
        }
        
        // Get all actions with their bindings (for UI display)
        public Dictionary<string, List<HotkeyEntry>> GetAllBindings()
        {
            var result = new Dictionary<string, List<HotkeyEntry>>();
            foreach (var kvp in _actionBindings)
            {
                result[kvp.Key] = new List<HotkeyEntry>(kvp.Value);
            }
            return result;
        }
        
        // Add a new binding to an action (auto-saves)
        public void AddBinding(HotkeyEntry entry)
        {
            if (!_actionBindings.ContainsKey(entry.ActionId))
            {
                _actionBindings[entry.ActionId] = new List<HotkeyEntry>();
            }
            
            _actionBindings[entry.ActionId].Add(entry);
            SaveHotkeys();
        }
        
        // Remove a specific binding from an action (auto-saves)
        public void RemoveBinding(string actionId, HotkeyEntry entry)
        {
            if (_actionBindings.TryGetValue(actionId, out var bindings))
            {
                bindings.Remove(entry);
                if (bindings.Count == 0)
                {
                    _actionBindings.Remove(actionId);
                }
                SaveHotkeys();
            }
        }
        
        // Remove all bindings for an action (auto-saves)
        public void RemoveAllBindings(string actionId)
        {
            if (_actionBindings.ContainsKey(actionId))
            {
                _actionBindings.Remove(actionId);
                SaveHotkeys();
            }
        }
        
        // Legacy method for backward compatibility - adds/replaces first binding
        public void SetHotkey(HotkeyEntry entry)
        {
            if (!_actionBindings.ContainsKey(entry.ActionId))
            {
                _actionBindings[entry.ActionId] = new List<HotkeyEntry>();
            }
            
            // Replace first binding or add new one
            if (_actionBindings[entry.ActionId].Count > 0)
            {
                _actionBindings[entry.ActionId][0] = entry;
            }
            else
            {
                _actionBindings[entry.ActionId].Add(entry);
            }
            
            SaveHotkeys();
        }
        
        // Handle keyboard input
        public bool HandleKeyDown(Key key, ModifierKeys modifiers)
        {
            if (!_isEnabled)
                return false;
                
            // Check all bindings for all actions
            foreach (var kvp in _actionBindings)
            {
                foreach (var binding in kvp.Value)
                {
                    if (binding.KeyboardKey == key && binding.MatchesKeyboardModifiers(modifiers))
                    {
                        TriggerAction(kvp.Key);
                        return true;
                    }
                }
            }
            
            return false;
        }
        
        // Handle gamepad input
        private void GamepadManager_ButtonsPressed(object? sender, List<string> pressedButtons)
        {
            if (!_isEnabled)
                return;
                
            // When global hotkeys are disabled, check if app has focus
            // (Gamepad events are always "global" so we need this check)
            if (!_globalHotkeysEnabled && !KeyboardShortcuts.IsOurApplicationActive())
            {
                return;
            }
            
            Console.WriteLine($"Gamepad buttons pressed: {string.Join(", ", pressedButtons)}");
                
            // Check all bindings for all actions
            foreach (var kvp in _actionBindings)
            {
                foreach (var binding in kvp.Value)
                {
                    if (binding.MatchesGamepadButtons(pressedButtons))
                    {
                        Console.WriteLine($"Gamepad matched action: {kvp.Key}");
                        // Invoke on UI thread
                        System.Windows.Application.Current.Dispatcher.Invoke(() =>
                        {
                            TriggerAction(kvp.Key);
                        });
                        return;
                    }
                }
            }
        }
        
        // Trigger an action by ID
        private void TriggerAction(string actionId)
        {
            // Check debounce - prevent triggering the same action too quickly
            if (_lastActionTime.TryGetValue(actionId, out DateTime lastTime))
            {
                double msSinceLastTrigger = (DateTime.Now - lastTime).TotalMilliseconds;
                if (msSinceLastTrigger < DEBOUNCE_MS)
                {
                    if (ConfigManager.Instance.GetLogExtraDebugStuff())
                    {
                        Console.WriteLine($"Hotkey {actionId} debounced ({msSinceLastTrigger:F0}ms since last trigger)");
                    }
                    return;
                }
            }
            
            _lastActionTime[actionId] = DateTime.Now;
            Console.WriteLine($"Hotkey triggered: {actionId}");
            
            switch (actionId)
            {
                case "start_stop":
                    StartStopRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "toggle_monitor":
                    MonitorToggleRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "toggle_chatbox":
                    ChatBoxToggleRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "toggle_settings":
                    SettingsToggleRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "toggle_log":
                    LogToggleRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "toggle_main_window":
                    MainWindowVisibilityToggleRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "clear_overlays":
                    ClearOverlaysRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "toggle_passthrough":
                    PassthroughToggleRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "toggle_overlay_mode":
                    OverlayModeToggleRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "prev_overlay_mode":
                    OverlayModePreviousRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "toggle_listen":
                    ListenToggleRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "view_in_browser":
                    ViewInBrowserRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "play_all_audio":
                    PlayAllAudioRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "snapshot":
                    SnapshotRequested?.Invoke(this, EventArgs.Empty);
                    break;
                case "capture_region_0":
                    CaptureRegionRequested?.Invoke(this, 0);
                    break;
                case "capture_region_1":
                    CaptureRegionRequested?.Invoke(this, 1);
                    break;
                case "capture_region_2":
                    CaptureRegionRequested?.Invoke(this, 2);
                    break;
                case "capture_region_3":
                    CaptureRegionRequested?.Invoke(this, 3);
                    break;
                case "capture_region_4":
                    CaptureRegionRequested?.Invoke(this, 4);
                    break;
                case "capture_region_5":
                    CaptureRegionRequested?.Invoke(this, 5);
                    break;
            }
        }
        
        // Reset to default hotkeys
        public void ResetToDefaults()
        {
            CreateDefaultHotkeys();
            Console.WriteLine("Hotkeys reset to defaults");
        }
        
        // Load hotkeys from file
        private void LoadHotkeys()
        {
            _actionBindings.Clear();
            bool migratedLegacyRegionReset = false;
            
            if (File.Exists(HOTKEYS_FILE))
            {
                try
                {
                    string[] lines = File.ReadAllLines(HOTKEYS_FILE);
                    
                    // First line is global hotkeys enabled flag
                    if (lines.Length > 0 && bool.TryParse(lines[0], out bool globalEnabled))
                    {
                        _globalHotkeysEnabled = globalEnabled;
                    }
                    
                    // Rest of lines are hotkey entries
                    int bindingCount = 0;
                    for (int i = 1; i < lines.Length; i++)
                    {
                        if (string.IsNullOrWhiteSpace(lines[i]))
                            continue;
                            
                        var entry = HotkeyEntry.Deserialize(lines[i]);
                        if (entry != null)
                        {
                            if (entry.ActionId == "capture_region_reset")
                            {
                                entry.ActionId = "capture_region_0";
                                entry.ActionName = "Capture Region 0";
                                migratedLegacyRegionReset = true;
                            }

                            if (!_actionBindings.ContainsKey(entry.ActionId))
                            {
                                _actionBindings[entry.ActionId] = new List<HotkeyEntry>();
                            }
                            _actionBindings[entry.ActionId].Add(entry);
                            bindingCount++;
                        }
                    }
                    
                    Console.WriteLine($"Loaded {bindingCount} hotkey bindings from {HOTKEYS_FILE}");
                    
                    // Backfill any new actions that were added since the file was last saved
                    BackfillMissingDefaults();

                    if (migratedLegacyRegionReset)
                    {
                        SaveHotkeys();
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"Error loading hotkeys: {ex.Message}");
                    CreateDefaultHotkeys();
                }
            }
            else
            {
                CreateDefaultHotkeys();
            }
        }
        
        // Save hotkeys to file
        public void SaveHotkeys()
        {
            try
            {
                List<string> lines = new List<string>();
                
                // First line is global hotkeys enabled flag
                lines.Add(_globalHotkeysEnabled.ToString());
                
                // Rest of lines are hotkey entries
                int bindingCount = 0;
                foreach (var kvp in _actionBindings)
                {
                    foreach (var binding in kvp.Value)
                    {
                        lines.Add(binding.Serialize());
                        bindingCount++;
                    }
                }
                
                File.WriteAllLines(HOTKEYS_FILE, lines);
                Console.WriteLine($"Saved {bindingCount} hotkey bindings to {HOTKEYS_FILE}");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Error saving hotkeys: {ex.Message}");
            }
        }
        
        // Create default hotkeys
        private void CreateDefaultHotkeys()
        {
            _actionBindings.Clear();
            _globalHotkeysEnabled = true;
            
            _actionBindings = GetDefaultBindings();
            
            SaveHotkeys();
            Console.WriteLine("Created default hotkeys");
        }
        
        // Add default bindings for any actions that don't yet exist in the loaded file.
        // This handles the case where new hotkey actions are added to the code but the
        // user already has an existing hotkeys.txt from a previous version.
        private void BackfillMissingDefaults()
        {
            var defaults = GetDefaultBindings();
            bool added = false;
            
            foreach (var kvp in defaults)
            {
                if (!_actionBindings.ContainsKey(kvp.Key))
                {
                    _actionBindings[kvp.Key] = kvp.Value;
                    Console.WriteLine($"Backfilled missing hotkey: {kvp.Key}");
                    added = true;
                }
            }
            
            if (added)
            {
                SaveHotkeys();
            }
        }
        
        // Returns the full set of default bindings (used by both CreateDefaultHotkeys and BackfillMissingDefaults)
        private Dictionary<string, List<HotkeyEntry>> GetDefaultBindings()
        {
            var defaults = new Dictionary<string, List<HotkeyEntry>>();
            
            var startStop = new HotkeyEntry("start_stop", "Start/Stop Live OCR") { KeyboardKey = Key.S, UseShift = true };
            defaults["start_stop"] = new List<HotkeyEntry> { startStop };
            
            var toggleMonitor = new HotkeyEntry("toggle_monitor", "Toggle Monitor Window") { KeyboardKey = Key.M, UseShift = true };
            defaults["toggle_monitor"] = new List<HotkeyEntry> { toggleMonitor };
            
            var toggleChatBox = new HotkeyEntry("toggle_chatbox", "Toggle Transcript") { KeyboardKey = Key.C, UseShift = true };
            defaults["toggle_chatbox"] = new List<HotkeyEntry> { toggleChatBox };
            
            var toggleSettings = new HotkeyEntry("toggle_settings", "Toggle Settings") { KeyboardKey = Key.E, UseShift = true };
            defaults["toggle_settings"] = new List<HotkeyEntry> { toggleSettings };
            
            var toggleLog = new HotkeyEntry("toggle_log", "Toggle Log") { KeyboardKey = Key.L, UseShift = true };
            defaults["toggle_log"] = new List<HotkeyEntry> { toggleLog };
            
            var toggleMainWindow = new HotkeyEntry("toggle_main_window", "Toggle Main Window") { KeyboardKey = Key.H, UseShift = true };
            defaults["toggle_main_window"] = new List<HotkeyEntry> { toggleMainWindow };
            
            var clearOverlays = new HotkeyEntry("clear_overlays", "Clear Overlays") { KeyboardKey = Key.X, UseShift = true };
            defaults["clear_overlays"] = new List<HotkeyEntry> { clearOverlays };
            
            var togglePassthrough = new HotkeyEntry("toggle_passthrough", "Toggle Passthrough") { KeyboardKey = Key.P, UseShift = true };
            defaults["toggle_passthrough"] = new List<HotkeyEntry> { togglePassthrough };
            
            var toggleOverlayMode = new HotkeyEntry("toggle_overlay_mode", "Next Overlay Mode") { KeyboardKey = Key.Tab };
            defaults["toggle_overlay_mode"] = new List<HotkeyEntry> { toggleOverlayMode };
            
            var prevOverlayMode = new HotkeyEntry("prev_overlay_mode", "Previous Overlay Mode");
            defaults["prev_overlay_mode"] = new List<HotkeyEntry> { prevOverlayMode };
            
            var toggleListen = new HotkeyEntry("toggle_listen", "Toggle Listen");
            defaults["toggle_listen"] = new List<HotkeyEntry> { toggleListen };
            
            var viewInBrowser = new HotkeyEntry("view_in_browser", "View in Browser") { KeyboardKey = Key.B, UseShift = true };
            defaults["view_in_browser"] = new List<HotkeyEntry> { viewInBrowser };
            
            var snapshot = new HotkeyEntry("snapshot", "Snapshot OCR") { KeyboardKey = Key.Z, UseShift = true };
            defaults["snapshot"] = new List<HotkeyEntry> { snapshot };

            var region0 = new HotkeyEntry("capture_region_0", "Capture Region 0") { KeyboardKey = Key.D0, UseShift = true };
            defaults["capture_region_0"] = new List<HotkeyEntry> { region0 };
            
            var region1 = new HotkeyEntry("capture_region_1", "Capture Region 1") { KeyboardKey = Key.D1, UseShift = true };
            defaults["capture_region_1"] = new List<HotkeyEntry> { region1 };
            
            var region2 = new HotkeyEntry("capture_region_2", "Capture Region 2") { KeyboardKey = Key.D2, UseShift = true };
            defaults["capture_region_2"] = new List<HotkeyEntry> { region2 };
            
            var region3 = new HotkeyEntry("capture_region_3", "Capture Region 3") { KeyboardKey = Key.D3, UseShift = true };
            defaults["capture_region_3"] = new List<HotkeyEntry> { region3 };
            
            var region4 = new HotkeyEntry("capture_region_4", "Capture Region 4") { KeyboardKey = Key.D4, UseShift = true };
            defaults["capture_region_4"] = new List<HotkeyEntry> { region4 };
            
            var region5 = new HotkeyEntry("capture_region_5", "Capture Region 5") { KeyboardKey = Key.D5, UseShift = true };
            defaults["capture_region_5"] = new List<HotkeyEntry> { region5 };
            
            // Play All Audio - No default key
            var playAllAudio = new HotkeyEntry("play_all_audio", "Play All Audio");
            defaults["play_all_audio"] = new List<HotkeyEntry> { playAllAudio };
            
            return defaults;
        }
    }
}


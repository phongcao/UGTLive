using System;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;

namespace UGTLive
{
    /// <summary>
    /// A separate top-level transparent window that hosts the WebView2 overlay.
    /// Being a distinct top-level HWND allows it to be hidden/shown independently
    /// from the MainWindow without affecting the capture frame or controls.
    /// When <c>WDA_EXCLUDEFROMCAPTURE</c> works on the system, the overlay is
    /// automatically invisible to capture without any hide/show.  Otherwise the
    /// caller can use <see cref="HideForCapture"/>/<see cref="ShowAfterCapture"/>
    /// for a flicker-free capture (only this overlay blinks, not the main UI).
    /// </summary>
    public partial class OverlayWindow : Window
    {
        // Win32 interop for capture exclusion and click-through
        [DllImport("user32.dll")]
        private static extern bool SetWindowDisplayAffinity(IntPtr hwnd, uint dwAffinity);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern int GetWindowLong(IntPtr hWnd, int nIndex);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);

        [DllImport("user32.dll")]
        private static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);

        [DllImport("user32.dll")]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        private const uint WDA_NONE = 0x00000000;
        private const uint WDA_EXCLUDEFROMCAPTURE = 0x00000011;

        private const int GWL_EXSTYLE = -20;
        private const int WS_EX_TRANSPARENT = 0x00000020;
        private const int WS_EX_LAYERED = 0x00080000;
        private const int WS_EX_TOOLWINDOW = 0x00000080;
        private const int WS_EX_NOACTIVATE = 0x08000000;

        private static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
        private const uint SWP_NOMOVE = 0x0002;
        private const uint SWP_NOSIZE = 0x0001;
        private const uint SWP_NOACTIVATE = 0x0010;
        private const uint SWP_SHOWWINDOW = 0x0040;

        private const int SW_HIDE = 0;
        private const int SW_SHOWNOACTIVATE = 4;

        private bool _webViewInitialized = false;
        private bool _clickThroughEnabled = false;
        private bool _captureExclusionActive = false;
        private bool _hiddenForCapture = false;

        public bool IsWebViewInitialized => _webViewInitialized;
        public WebView2 WebView => overlayWebView;

        /// <summary>Whether WDA_EXCLUDEFROMCAPTURE is active and working.</summary>
        public bool IsCaptureExclusionActive => _captureExclusionActive;

        /// <summary>Fires once the WebView2 CoreWebView2 is ready and the initial page is loaded.</summary>
        public event EventHandler? WebViewReady;

        public OverlayWindow()
        {
            InitializeComponent();
            SourceInitialized += OverlayWindow_SourceInitialized;
        }

        private void OverlayWindow_SourceInitialized(object? sender, EventArgs e)
        {
            IntPtr hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd == IntPtr.Zero) return;

            // Make the window a tool window (no taskbar entry) and non-activating
            int exStyle = GetWindowLong(hwnd, GWL_EXSTYLE);
            exStyle |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
            SetWindowLong(hwnd, GWL_EXSTYLE, exStyle);

            // Do NOT apply WDA_EXCLUDEFROMCAPTURE by default — on many systems/GPU drivers
            // (e.g. NVIDIA RTX) it makes the window invisible to the *user* as well.
            // Instead we rely on HideForCapture()/ShowAfterCapture() in TryCopyCaptureRectToBitmap.
            // _captureExclusionActive defaults to false so the hide/show path is used.

            // Ensure topmost
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        }

        /// <summary>
        /// Apply or remove the capture exclusion based on the current config setting.
        /// Returns true if WDA_EXCLUDEFROMCAPTURE was successfully applied.
        /// </summary>
        public bool ApplyCaptureExclusion()
        {
            IntPtr hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd == IntPtr.Zero) return false;

            bool visibleInScreenshots = ConfigManager.Instance.GetWindowsVisibleInScreenshots();
            if (visibleInScreenshots)
            {
                SetWindowDisplayAffinity(hwnd, WDA_NONE);
                _captureExclusionActive = false;
                Console.WriteLine("OverlayWindow capture exclusion: disabled (visible in screenshots)");
                return false;
            }

            bool success = SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE);
            _captureExclusionActive = success;
            Console.WriteLine($"OverlayWindow capture exclusion: WDA_EXCLUDEFROMCAPTURE success={success}");
            return success;
        }

        /// <summary>
        /// Disable WDA_EXCLUDEFROMCAPTURE if it was making the overlay invisible to the user.
        /// The caller should use HideForCapture/ShowAfterCapture instead.
        /// </summary>
        public void DisableCaptureExclusion()
        {
            IntPtr hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd == IntPtr.Zero) return;

            SetWindowDisplayAffinity(hwnd, WDA_NONE);
            _captureExclusionActive = false;
            Console.WriteLine("OverlayWindow capture exclusion: force-disabled (fallback to hide/show)");
        }

        /// <summary>
        /// Quickly hide the overlay window before a screen capture.
        /// Only needed when WDA_EXCLUDEFROMCAPTURE is not active.
        /// Uses Win32 ShowWindow for speed — no WPF layout pass.
        /// </summary>
        public void HideForCapture()
        {
            if (_captureExclusionActive) return; // Not needed — OS already hides it
            IntPtr hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd == IntPtr.Zero) return;
            ShowWindow(hwnd, SW_HIDE);
            _hiddenForCapture = true;
        }

        /// <summary>
        /// Restore the overlay window after a screen capture.
        /// </summary>
        public void ShowAfterCapture()
        {
            if (!_hiddenForCapture) return;
            IntPtr hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd == IntPtr.Zero) return;
            ShowWindow(hwnd, SW_SHOWNOACTIVATE);
            // Re-assert topmost since ShowWindow can change z-order
            SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            _hiddenForCapture = false;
        }

        /// <summary>
        /// Enable or disable OS-level click-through (WS_EX_TRANSPARENT).
        /// When enabled, all mouse events pass through to the window behind.
        /// </summary>
        public void SetClickThrough(bool enabled)
        {
            IntPtr hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd == IntPtr.Zero) return;

            int exStyle = GetWindowLong(hwnd, GWL_EXSTYLE);

            if (enabled)
            {
                exStyle |= WS_EX_TRANSPARENT | WS_EX_LAYERED;
            }
            else
            {
                exStyle &= ~WS_EX_TRANSPARENT;
                // Keep WS_EX_LAYERED — it's needed for AllowsTransparency anyway
            }

            SetWindowLong(hwnd, GWL_EXSTYLE, exStyle);
            _clickThroughEnabled = enabled;
        }

        /// <summary>
        /// Initialize the WebView2 environment and set up event handlers.
        /// Call this after the window is loaded.
        /// </summary>
        public async Task InitializeWebViewAsync(
            EventHandler<CoreWebView2WebMessageReceivedEventArgs>? messageHandler,
            EventHandler<CoreWebView2ContextMenuRequestedEventArgs>? contextMenuHandler)
        {
            try
            {
                var environment = await WebViewEnvironmentManager.GetEnvironmentAsync();

                overlayWebView.DefaultBackgroundColor = System.Drawing.Color.Transparent;
                await overlayWebView.EnsureCoreWebView2Async(environment);

                if (overlayWebView.CoreWebView2 != null)
                {
                    overlayWebView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
                    overlayWebView.CoreWebView2.Settings.AreDevToolsEnabled = false;
                    overlayWebView.CoreWebView2.Settings.IsZoomControlEnabled = false;
                    overlayWebView.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = false;

                    if (messageHandler != null)
                        overlayWebView.CoreWebView2.WebMessageReceived += messageHandler;
                    if (contextMenuHandler != null)
                        overlayWebView.CoreWebView2.ContextMenuRequested += contextMenuHandler;

                    _webViewInitialized = true;
                    Console.WriteLine("OverlayWindow WebView2 initialized successfully");

                    WebViewReady?.Invoke(this, EventArgs.Empty);
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"Error initializing OverlayWindow WebView2: {ex.Message}");
            }
        }

        /// <summary>
        /// Navigate the WebView2 to the given HTML string.
        /// </summary>
        public void NavigateToHtml(string html)
        {
            if (!_webViewInitialized || overlayWebView?.CoreWebView2 == null) return;
            overlayWebView.CoreWebView2.NavigateToString(html);
        }

        /// <summary>
        /// Execute a JavaScript snippet inside the WebView2.
        /// </summary>
        public void ExecuteScript(string script)
        {
            if (!_webViewInitialized || overlayWebView?.CoreWebView2 == null) return;
            overlayWebView.CoreWebView2.ExecuteScriptAsync(script);
        }

        /// <summary>
        /// Execute a JavaScript snippet and return the result.
        /// </summary>
        public async Task<string> ExecuteScriptWithResultAsync(string script)
        {
            if (!_webViewInitialized || overlayWebView?.CoreWebView2 == null) return string.Empty;
            return await overlayWebView.CoreWebView2.ExecuteScriptAsync(script);
        }

        /// <summary>
        /// Reposition and resize this window to match the given screen rectangle.
        /// Uses physical screen pixels — matches the captureRect from MainWindow.
        /// </summary>
        public void UpdatePosition(double left, double top, double width, double height)
        {
            // WPF uses device-independent units; for a transparent topmost window
            // that tracks physical pixel coordinates, we need to convert.
            var source = PresentationSource.FromVisual(this);
            double dpiScaleX = source?.CompositionTarget?.TransformFromDevice.M11 ?? 1.0;
            double dpiScaleY = source?.CompositionTarget?.TransformFromDevice.M22 ?? 1.0;

            Left = left * dpiScaleX;
            Top = top * dpiScaleY;
            Width = width * dpiScaleX;
            Height = height * dpiScaleY;
        }

        public void BringToFront()
        {
            IntPtr hwnd = new WindowInteropHelper(this).Handle;
            if (hwnd != IntPtr.Zero)
            {
                SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
            }
        }

        /// <summary>
        /// Translate a point from the WebView's client coordinates to screen coordinates.
        /// Used for context menu positioning.
        /// </summary>
        public System.Windows.Point WebViewClientToScreen(double clientX, double clientY)
        {
            System.Windows.Point contentPoint = new System.Windows.Point(clientX, clientY);
            System.Windows.Point relativeToWindow = overlayWebView.TranslatePoint(contentPoint, this);
            return PointToScreen(relativeToWindow);
        }

        protected override void OnActivated(EventArgs e)
        {
            // Immediately deactivate — this window should never steal focus
            base.OnActivated(e);
        }
    }
}

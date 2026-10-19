using System.ComponentModel;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace ColpilotColmonWindows;

internal static class Program
{
    [STAThread]
    private static int Main()
    {
        ApplicationConfiguration.Initialize();
        var reporter = new HostReporter();
        using var context = new TaskbarApplicationContext(reporter);
        var commandThread = new Thread(() => ReadCommands(context))
        {
            IsBackground = true,
            Name = "colmon-native-command-reader"
        };
        commandThread.Start();
        Application.Run(context);
        return 0;
    }

    private static void ReadCommands(TaskbarApplicationContext context)
    {
        try
        {
            using var input = new StreamReader(Console.OpenStandardInput());
            while (true)
            {
                var line = input.ReadLine();
                if (line is null)
                {
                    context.RequestShutdown();
                    return;
                }

                using var document = JsonDocument.Parse(line);
                var root = document.RootElement;
                var type = root.GetProperty("type").GetString();
                switch (type)
                {
                    case "state":
                        context.ApplyState(root.GetProperty("state").Clone());
                        break;
                    case "shutdown":
                        context.RequestShutdown();
                        return;
                    default:
                        throw new InvalidOperationException($"未知的 colmon 原生窗口命令：{type}");
                }
            }
        }
        catch (Exception exception)
        {
            context.ReportFailure(exception);
        }
    }
}

internal sealed class HostReporter
{
    private readonly object _lock = new();
    private static readonly JsonSerializerOptions Options = new(JsonSerializerDefaults.Web);

    public void Write(object message)
    {
        lock (_lock)
        {
            Console.WriteLine(JsonSerializer.Serialize(message, Options));
            Console.Out.Flush();
        }
    }
}

internal sealed class TaskbarApplicationContext : ApplicationContext
{
    private readonly HostReporter _reporter;
    private readonly TaskbarMetricForm[] _forms;
    private readonly System.Windows.Forms.Timer _readyTimer;
    private bool _readySent;
    private bool _shutdownRequested;

    public TaskbarApplicationContext(HostReporter reporter)
    {
        _reporter = reporter;
        _forms =
        [
            new TaskbarMetricForm("codex-weekly", 0, true, reporter),
            new TaskbarMetricForm("codex-five-hour", 1, true, reporter),
            new TaskbarMetricForm("codex-tokens-today", 2, false, reporter)
        ];
        foreach (var form in _forms)
        {
            form.FormClosed += (_, _) => RequestShutdown();
            form.Show();
        }

        _readyTimer = new System.Windows.Forms.Timer { Interval = 200 };
        _readyTimer.Tick += (_, _) => CheckReady();
        _readyTimer.Start();
    }

    public void ApplyState(JsonElement state)
    {
        InvokeOnUiThread(() =>
        {
            var windowId = state.GetProperty("windowId").GetString();
            var form = _forms.SingleOrDefault(item => item.WindowId == windowId);
            form?.ApplyState(state);
        });
    }

    public void RequestShutdown()
    {
        if (_shutdownRequested)
        {
            return;
        }

        _shutdownRequested = true;
        InvokeOnUiThread(CloseApplication);
    }

    public void ReportFailure(Exception exception)
    {
        _reporter.Write(new
        {
            type = "error",
            detail = exception.Message
        });
        RequestShutdown();
    }

    private void CheckReady()
    {
        if (_readySent || !_forms.All(form => form.Attached))
        {
            return;
        }

        _readySent = true;
        _readyTimer.Stop();
        _reporter.Write(new
        {
            type = "ready",
            processId = Environment.ProcessId,
            windowCount = _forms.Length
        });
    }

    private void InvokeOnUiThread(Action action)
    {
        if (_forms.Length == 0 || _forms[0].IsDisposed)
        {
            return;
        }

        if (_forms[0].InvokeRequired)
        {
            _forms[0].BeginInvoke(action);
            return;
        }

        action();
    }

    private void CloseApplication()
    {
        _readyTimer.Stop();
        foreach (var form in _forms)
        {
            if (!form.IsDisposed)
            {
                form.Close();
            }
        }

        ExitThread();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _readyTimer.Dispose();
        }

        base.Dispose(disposing);
    }
}

internal sealed class TaskbarMetricForm : Form
{
    private const int LogicalQuotaHeight = 42;
    private const int LogicalCountHeight = 32;
    private const int LogicalWidth = 105;
    private const int WindowGap = 4;
    private const int WmDisplayChange = 0x007E;
    private const int WmSettingChange = 0x001A;
    private const int WmDpiChanged = 0x02E0;

    private readonly HostReporter _reporter;
    private readonly int _slotIndex;
    private readonly bool _quota;
    private readonly TaskbarDisplayControl _view;
    private readonly System.Windows.Forms.Timer _recoveryTimer;
    private readonly uint _taskbarCreatedMessage;
    private nint _taskbar;
    private nint _notifyArea;
    private bool _attached;

    public TaskbarMetricForm(string windowId, int slotIndex, bool quota, HostReporter reporter)
    {
        WindowId = windowId;
        _slotIndex = slotIndex;
        _quota = quota;
        _reporter = reporter;
        _view = new TaskbarDisplayControl(quota)
        {
            Dock = DockStyle.Fill
        };
        _recoveryTimer = new System.Windows.Forms.Timer { Interval = 1000 };
        _recoveryTimer.Tick += (_, _) => AttachAndPlace("recovery-timer");
        _taskbarCreatedMessage = NativeMethods.RegisterWindowMessage("TaskbarCreated");

        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.Manual;
        BackColor = Color.FromArgb(1, 0, 0);
        TransparencyKey = BackColor;
        ForeColor = Color.White;
        Font = new Font("Microsoft YaHei", 9F, FontStyle.Regular, GraphicsUnit.Point);
        AutoScaleMode = AutoScaleMode.Dpi;
        DoubleBuffered = true;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint, true);
        _view.Font = Font;
        _view.ForeColor = ForeColor;
        Controls.Add(_view);
    }

    public string WindowId { get; }
    public bool Attached => _attached;

    protected override bool ShowWithoutActivation => true;

    protected override CreateParams CreateParams
    {
        get
        {
            var parameters = base.CreateParams;
            parameters.ExStyle |= (int)(NativeMethods.WsExToolWindow | NativeMethods.WsExNoActivate);
            return parameters;
        }
    }

    protected override void OnLoad(EventArgs eventArgs)
    {
        base.OnLoad(eventArgs);
        AttachAndPlace("load");
        _recoveryTimer.Start();
    }

    public void ApplyState(JsonElement state)
    {
        _view.ApplyState(state);
        AttachAndPlace("data-change");
    }

    private void AttachAndPlace(string reason)
    {
        if (!IsHandleCreated)
        {
            return;
        }

        var taskbar = NativeMethods.FindWindow("Shell_TrayWnd", null);
        if (taskbar == 0)
        {
            _attached = false;
            return;
        }

        if (!_attached || taskbar != _taskbar || NativeMethods.GetAncestor(Handle, NativeMethods.GaParent) != taskbar)
        {
            _taskbar = taskbar;
            _notifyArea = NativeMethods.FindWindowEx(taskbar, 0, "TrayNotifyWnd", null);
            NativeMethods.SetParent(Handle, taskbar);
            _attached = NativeMethods.GetAncestor(Handle, NativeMethods.GaParent) == taskbar;
        }

        if (!_attached)
        {
            return;
        }

        var taskbarRect = NativeMethods.Rect(_taskbar);
        var notifyRect = _notifyArea == 0 ? default : NativeMethods.Rect(_notifyArea);
        var dpi = Math.Max(96u, NativeMethods.GetDpiForWindow(_taskbar));
        var scale = dpi / 96F;
        var width = Math.Max(1, (int)Math.Round(LogicalWidth * scale));
        var logicalHeight = _quota ? LogicalQuotaHeight : LogicalCountHeight;
        var height = Math.Clamp((int)Math.Round(logicalHeight * scale), 24,
            Math.Max(24, taskbarRect.Height - (int)Math.Round(4 * scale)));
        var notifyLeft = _notifyArea == 0
            ? taskbarRect.Right - (int)Math.Round(110 * scale)
            : notifyRect.Left;
        var gap = Math.Max(2, (int)Math.Round(WindowGap * scale));
        var occupied = TaskbarPlacement.ExternalWindows(_taskbar, Handle, taskbarRect);
        var anchorX = TaskbarPlacement.FindAvailableX(
            taskbarRect.Left,
            taskbarRect.Width,
            notifyLeft,
            width,
            gap,
            occupied.Select(window => window.Rectangle));
        var x = Math.Clamp(anchorX - _slotIndex * (width + gap), 0, Math.Max(0, taskbarRect.Width - width));
        var y = Math.Clamp((taskbarRect.Height - height) / 2, 0, Math.Max(0, taskbarRect.Height - height));
        if (!NativeMethods.MoveWindow(Handle, x, y, width, height, true))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), $"无法调整任务栏窗口位置：{WindowId}");
        }
        _attached = NativeMethods.GetAncestor(Handle, NativeMethods.GaParent) == _taskbar;
        if (!_attached)
        {
            return;
        }

        _reporter.Write(new
        {
            type = "window",
            windowId = WindowId,
            attached = true,
            processId = Environment.ProcessId,
            visible = NativeMethods.IsWindowVisible(Handle) &&
                _view.IsHandleCreated && NativeMethods.IsWindowVisible(_view.Handle),
            handles = new
            {
                app = $"0x{Handle.ToInt64():X}",
                content = _view.IsHandleCreated ? $"0x{_view.Handle.ToInt64():X}" : null,
                taskbar = $"0x{_taskbar.ToInt64():X}",
                notificationArea = _notifyArea == 0 ? null : $"0x{_notifyArea.ToInt64():X}"
            },
            rectangles = new
            {
                app = NativeMethods.Rect(Handle),
                taskbar = taskbarRect,
                notificationArea = _notifyArea == 0 ? (NativeRect?)null : notifyRect
            },
            placement = new { x, y, width, height, gap, reason },
            dpi
        });
    }

    protected override void WndProc(ref Message message)
    {
        base.WndProc(ref message);
        var messageCode = message.Msg;
        if ((uint)messageCode == _taskbarCreatedMessage || messageCode is WmDisplayChange or WmSettingChange or WmDpiChanged)
        {
            BeginInvoke(() => AttachAndPlace($"window-message-{messageCode:X}"));
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _recoveryTimer.Stop();
            _recoveryTimer.Dispose();
        }

        base.Dispose(disposing);
    }
}

internal sealed class TaskbarDisplayControl : Control
{
    private readonly bool _quota;
    private string _title = "等待数据";
    private string _percentageText = "--%";
    private string _resetText = "--";
    private string _numberText = "--";
    private decimal? _remaining;

    public TaskbarDisplayControl(bool quota)
    {
        _quota = quota;
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer |
            ControlStyles.ResizeRedraw | ControlStyles.SupportsTransparentBackColor | ControlStyles.UserPaint, true);
        ForeColor = Color.White;
        BackColor = Color.Transparent;
    }

    public void ApplyState(JsonElement state)
    {
        var display = state.TryGetProperty("display", out var displayElement) && displayElement.ValueKind == JsonValueKind.Object
            ? displayElement
            : default;
        _title = ReadText(display, "title") ?? ReadText(state, "title") ?? _title;
        if (_quota)
        {
            _percentageText = ReadText(display, "percentageText") ?? ReadText(state, "value") ?? "--%";
            _resetText = ReadText(display, "resetRemainingText") ?? "--";
            _remaining = ParsePercentage(_percentageText);
        }
        else
        {
            _numberText = FormatNumber(ReadText(display, "numberText") ?? ReadText(state, "value"));
        }

        Invalidate();
    }

    protected override void OnPaint(PaintEventArgs eventArgs)
    {
        base.OnPaint(eventArgs);
        if (_quota)
        {
            PaintQuota(eventArgs.Graphics);
            return;
        }

        var titleHeight = Height / 2;
        DrawCentered(eventArgs.Graphics, _title, new Rectangle(0, 0, Width, titleHeight), ForeColor);
        DrawCentered(eventArgs.Graphics, _numberText, new Rectangle(0, titleHeight, Width, Height - titleHeight), ForeColor);
    }

    private void PaintQuota(Graphics graphics)
    {
        var logicalHeight = 42;
        var titleHeight = Height * 15 / logicalHeight;
        var quotaHeight = Height * 10 / logicalHeight;
        DrawCentered(graphics, _title, new Rectangle(0, 0, Width, titleHeight), ForeColor);
        var scale = Math.Max(1F, DeviceDpi / 96F);
        var barHeight = Math.Max(4, (int)Math.Round(6 * scale));
        var barTop = titleHeight + (quotaHeight - barHeight) / 2;
        var bar = new Rectangle(0, barTop, Math.Max(0, Width), barHeight);
        if (bar.Width > 0)
        {
            using var trackBrush = new SolidBrush(Color.FromArgb(82, 82, 82));
            graphics.FillRectangle(trackBrush, bar);
            if (_remaining is > 0M)
            {
                var fillWidth = Math.Clamp((int)Math.Round(bar.Width * (double)(_remaining.Value / 100M)), 1, bar.Width);
                using var fillBrush = new SolidBrush(_remaining < 10M
                    ? Color.FromArgb(255, 76, 76)
                    : Color.FromArgb(0, 120, 215));
                graphics.FillRectangle(fillBrush, new Rectangle(bar.Left, bar.Top, fillWidth, bar.Height));
            }
        }

        var resetTop = titleHeight + quotaHeight;
        var line = $"{_percentageText}{new string(' ', 4)}{_resetText}";
        DrawCentered(graphics, line, new Rectangle(0, resetTop, Width, Math.Max(0, Height - resetTop)),
            _remaining is < 10M ? Color.FromArgb(255, 76, 76) : ForeColor);
    }

    private void DrawCentered(Graphics graphics, string text, Rectangle rectangle, Color color)
    {
        TextRenderer.DrawText(graphics, text, Font, rectangle, color,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter |
            TextFormatFlags.EndEllipsis | TextFormatFlags.SingleLine | TextFormatFlags.NoPadding);
    }

    private static string? ReadText(JsonElement element, string propertyName)
    {
        if (element.ValueKind != JsonValueKind.Object || !element.TryGetProperty(propertyName, out var property))
        {
            return null;
        }

        return property.ValueKind == JsonValueKind.String ? property.GetString() : property.ToString();
    }

    private static decimal? ParsePercentage(string text)
    {
        var normalized = text.Trim().TrimEnd('%');
        return decimal.TryParse(normalized, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)
            ? Math.Clamp(value, 0M, 100M)
            : null;
    }

    private static string FormatNumber(string? text)
    {
        return long.TryParse(text?.Replace(",", ""), NumberStyles.Integer, CultureInfo.InvariantCulture, out var value)
            ? value.ToString("N0", CultureInfo.InvariantCulture)
            : "--";
    }
}

internal static class TaskbarPlacement
{
    public static IReadOnlyList<OccupiedTaskbarWindow> ExternalWindows(
        nint taskbar,
        nint ownWindow,
        NativeRect taskbarRectangle)
    {
        NativeMethods.GetWindowThreadProcessId(taskbar, out var explorerProcessId);
        var windows = new List<OccupiedTaskbarWindow>();
        for (nint child = 0; ;)
        {
            child = NativeMethods.FindWindowEx(taskbar, child, null, null);
            if (child == 0) break;
            if (child == ownWindow || !NativeMethods.IsWindowVisible(child)) continue;
            NativeMethods.GetWindowThreadProcessId(child, out var processId);
            if (processId == 0 || processId == explorerProcessId || processId == Environment.ProcessId) continue;
            if (!NativeMethods.GetWindowRect(child, out var rectangle) || !Intersects(rectangle, taskbarRectangle)) continue;
            windows.Add(new OccupiedTaskbarWindow(child, processId, rectangle));
        }

        return windows;
    }

    public static int FindAvailableX(
        int taskbarLeft,
        int taskbarWidth,
        int notificationLeft,
        int windowWidth,
        int gap,
        IEnumerable<NativeRect> occupiedAbsoluteRectangles)
    {
        var occupied = occupiedAbsoluteRectangles
            .Select(rectangle => new NativeRect(
                rectangle.Left - taskbarLeft,
                rectangle.Top,
                rectangle.Right - taskbarLeft,
                rectangle.Bottom))
            .Where(rectangle => rectangle.Width > 0)
            .ToArray();
        var right = notificationLeft - taskbarLeft - gap;
        for (var attempts = 0; attempts <= occupied.Length; attempts++)
        {
            var left = right - windowWidth;
            var conflict = occupied
                .Where(rectangle => left < rectangle.Right && right > rectangle.Left)
                .OrderByDescending(rectangle => rectangle.Left)
                .FirstOrDefault();
            if (conflict.Width <= 0)
                return Math.Clamp(left, 0, Math.Max(0, taskbarWidth - windowWidth));
            right = conflict.Left - gap;
        }

        return Math.Clamp(right - windowWidth, 0, Math.Max(0, taskbarWidth - windowWidth));
    }

    private static bool Intersects(NativeRect left, NativeRect right) =>
        left.Left < right.Right && left.Right > right.Left && left.Top < right.Bottom && left.Bottom > right.Top;
}

internal sealed record OccupiedTaskbarWindow(nint Handle, uint ProcessId, NativeRect Rectangle);

internal static partial class NativeMethods
{
    internal const long WsExToolWindow = 0x00000080L;
    internal const long WsExNoActivate = 0x08000000L;
    internal const uint GaParent = 1;

    [LibraryImport("user32.dll", EntryPoint = "FindWindowW", StringMarshalling = StringMarshalling.Utf16)]
    internal static partial nint FindWindow(string className, string? windowName);

    [LibraryImport("user32.dll", EntryPoint = "FindWindowExW", StringMarshalling = StringMarshalling.Utf16)]
    internal static partial nint FindWindowEx(nint parent, nint childAfter, string? className, string? windowName);

    [LibraryImport("user32.dll", SetLastError = true)]
    internal static partial nint SetParent(nint child, nint newParent);

    [LibraryImport("user32.dll")]
    internal static partial nint GetAncestor(nint window, uint flags);

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool GetWindowRect(nint window, out NativeRect rect);

    [LibraryImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool MoveWindow(nint window, int x, int y, int width, int height, [MarshalAs(UnmanagedType.Bool)] bool repaint);

    [LibraryImport("user32.dll", EntryPoint = "RegisterWindowMessageW", StringMarshalling = StringMarshalling.Utf16)]
    internal static partial uint RegisterWindowMessage(string value);

    [LibraryImport("user32.dll")]
    internal static partial uint GetDpiForWindow(nint window);

    [LibraryImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static partial bool IsWindowVisible(nint window);

    [LibraryImport("user32.dll", SetLastError = true)]
    internal static partial uint GetWindowThreadProcessId(nint window, out uint processId);

    internal static NativeRect Rect(nint window)
    {
        if (window == 0 || !GetWindowRect(window, out var rect))
            throw new InvalidOperationException($"无法读取窗口矩形：0x{window.ToInt64():X}");
        return rect;
    }
}

[StructLayout(LayoutKind.Sequential)]
internal readonly record struct NativeRect(int Left, int Top, int Right, int Bottom)
{
    public int Width => Right - Left;
    public int Height => Bottom - Top;
}

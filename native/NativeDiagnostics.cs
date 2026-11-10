using System.Collections.Concurrent;
using System.Diagnostics;
using Serilog;

namespace ColpilotColmonWindows;

internal static class NativeDiagnostics
{
    private static readonly ConcurrentDictionary<int, (string Stage, long Started)> Operations = new();
    private static long _lastUiTick = Stopwatch.GetTimestamp();
    private static int _checks;
    private static int _outputBlocked;

    public static void Initialize()
    {
        var directory = Environment.GetEnvironmentVariable("COLMON_LOG_DIRECTORY")
            ?? throw new InvalidOperationException("缺少 COLMON_LOG_DIRECTORY");
        Directory.CreateDirectory(directory);
        Serilog.Debugging.SelfLog.Enable(Console.Error);
        Log.Logger = new LoggerConfiguration()
            .Enrich.WithProperty("ProcessId", Environment.ProcessId)
            .WriteTo.File(Path.Combine(directory, "native-.log"),
                rollingInterval: RollingInterval.Day,
                fileSizeLimitBytes: 5 * 1024 * 1024, rollOnFileSizeLimit: true,
                retainedFileCountLimit: 14,
                outputTemplate: "{Timestamp:yyyy-MM-ddTHH:mm:ss.fffzzz} [{Level:u3}] pid={ProcessId} {Message:lj}{NewLine}{Exception}")
            .CreateLogger();
        AppDomain.CurrentDomain.UnhandledException += (_, arguments) =>
        {
            Log.Fatal(arguments.ExceptionObject as Exception, "窗口宿主未处理异常");
            Log.CloseAndFlush();
        };
        Log.Information("窗口宿主启动 executable={Executable} runtime={Runtime} os={OS}",
            Environment.ProcessPath, Environment.Version, Environment.OSVersion);
    }

    public static void UiTick() => Interlocked.Exchange(ref _lastUiTick, Stopwatch.GetTimestamp());

    public static void Check(object? state)
    {
        var age = Stopwatch.GetElapsedTime(Interlocked.Read(ref _lastUiTick)).TotalSeconds;
        var operations = Operations.Select(item => new
        {
            threadId = item.Key, stage = item.Value.Stage,
            elapsedSeconds = Stopwatch.GetElapsedTime(item.Value.Started).TotalSeconds
        }).ToArray();
        var outputBlocked = operations.Any(item => item.elapsedSeconds > 5 &&
            item.stage is "Console.WriteLine" or "Console.Out.Flush");
        if (outputBlocked)
        {
            if (Interlocked.Exchange(ref _outputBlocked, 1) == 0)
            {
                Log.Error("宿主输出阻塞超过 5 秒 uiAgeSeconds={Age:F1} operations={@Operations}", age, operations);
            }
        }
        else
        {
            Interlocked.Exchange(ref _outputBlocked, 0);
        }
        if (age > 5 || operations.Any(item => item.elapsedSeconds > 5))
        {
            Log.Error("窗口宿主响应异常 uiAgeSeconds={Age:F1} operations={@Operations}", age, operations);
        }
        else if (Interlocked.Increment(ref _checks) % 12 == 1)
        {
            Log.Information("窗口宿主定期检查 uiAgeSeconds={Age:F1} operations={@Operations}", age, operations);
        }
    }

    public static Operation Begin(string stage) => new(stage);

    internal sealed class Operation : IDisposable
    {
        private readonly int _threadId = Environment.CurrentManagedThreadId;
        private readonly long _started = Stopwatch.GetTimestamp();
        private readonly string _name;
        private readonly (string Stage, long Started)? _previous;

        public Operation(string name)
        {
            _name = name;
            if (Operations.TryGetValue(_threadId, out var previous)) _previous = previous;
            Stage = name;
        }

        public string Stage { set => Operations[_threadId] = (value, _started); }

        public void Dispose()
        {
            if (_previous.HasValue) Operations[_threadId] = _previous.Value;
            else Operations.TryRemove(_threadId, out _);
            var elapsed = Stopwatch.GetElapsedTime(_started).TotalMilliseconds;
            if (elapsed > 250) Log.Warning("窗口宿主操作耗时 operation={Operation} elapsedMs={Elapsed:F1}", _name, elapsed);
        }
    }
}

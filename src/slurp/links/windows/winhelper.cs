// slurp helper: the small program Windows starts for slurp:// links (and at login, for the tray).
// The setup compiles it on the user's machine (PowerShell's Add-Type, .NET Framework 4, C# 5): no binary is shipped.
// It is a window program (no console window, no Windows Terminal tab) and does three things:
//   slurp-open.exe slurp://...   puts the link into the inbox of the tray (a file), starts the tray if it does not run
//   slurp-open.exe [--tray]      starts the tray if it does not run (else: the tray shows its window)
//   slurp-open.exe --feed PID    waits until the console of process PID shows a password prompt (ssh asks there:
//                                it reads the console, not stdin), then types the line on its stdin and Enter
//   slurp-open.exe --watch PID   waits until the console of process PID showed a password prompt and then something
//                                else (the shell's prompt): the user logged in there (exit 0)
// The tray (slurp-helper.ps1) runs in a PowerShell started without a console window (CREATE_NO_WINDOW). Not
// "hidden": Windows would apply that state to the first window the tray shows.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

public static class SlurpOpen
{
  [DllImport("kernel32.dll", SetLastError = true)] static extern bool AttachConsole(uint pid);
  [DllImport("kernel32.dll", SetLastError = true)] static extern bool FreeConsole();
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
  [DllImport("kernel32.dll", SetLastError = true)] static extern bool CloseHandle(IntPtr handle);
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern bool WriteConsoleInputW(IntPtr handle, INPUT_RECORD[] records, uint count, out uint written);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern short VkKeyScanW(char ch);
  [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetConsoleScreenBufferInfo(IntPtr handle, out SCREEN_INFO info);
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern bool ReadConsoleOutputCharacterW(IntPtr handle, [Out] char[] buffer, uint length, uint coord, out uint read);
  [DllImport("user32.dll")] static extern bool AllowSetForegroundWindow(int pid);
  [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd, int cmd);
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);

  [StructLayout(LayoutKind.Explicit)]
  struct KEY_EVENT_RECORD
  {
    [FieldOffset(0)] public int KeyDown;
    [FieldOffset(4)] public ushort RepeatCount;
    [FieldOffset(6)] public ushort VirtualKeyCode;
    [FieldOffset(8)] public ushort VirtualScanCode;
    [FieldOffset(10)] public ushort UnicodeChar; // (not char: that would be marshalled as one byte)
    [FieldOffset(12)] public uint ControlKeyState;
  }

  [StructLayout(LayoutKind.Explicit)]
  struct INPUT_RECORD
  {
    [FieldOffset(0)] public ushort EventType;
    [FieldOffset(4)] public KEY_EVENT_RECORD KeyEvent;
  }

  [StructLayout(LayoutKind.Sequential)]
  struct SCREEN_INFO // CONSOLE_SCREEN_BUFFER_INFO
  {
    public short SizeX, SizeY, CursorX, CursorY;
    public ushort Attributes;
    public short Left, Top, Right, Bottom, MaxX, MaxY;
  }

  const string TrayMutex = "Local\\slurp-helper-tray";

  public static string Folder()
  {
    return AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
  }

  [STAThread]
  public static int Main(string[] args)
  {
    try
    {
      if (args.Length >= 2 && args[0] == "--watch")
        return Watch(uint.Parse(args[1]), args.Length >= 3 ? int.Parse(args[2]) : 300);
      if (args.Length >= 2 && args[0] == "--feed")
        return Feed(uint.Parse(args[1]), Console.In.ReadLine() ?? "", args.Length >= 3 ? int.Parse(args[2]) : 60);
      try { AllowSetForegroundWindow(-1); } catch { }
      string message = "show";
      if (args.Length > 0 && args[0].StartsWith("slurp:", StringComparison.OrdinalIgnoreCase))
        message = args[0];
      else if (args.Length > 0 && args[0] == "--tray")
        message = "";
      bool running = TrayRuns();
      if (message != "" && (running || message != "show"))
        Post(message);
      if (!running)
        StartTray();
      return 0;
    }
    catch (Exception e)
    {
      try { File.WriteAllText(Path.Combine(Folder(), "open-error.log"), e.ToString()); } catch { }
      return 1;
    }
  }

  static bool TrayRuns()
  {
    Mutex m;
    if (Mutex.TryOpenExisting(TrayMutex, out m)) { m.Dispose(); return true; }
    return false;
  }

  /// <summary>A message for the tray: a file in its inbox (only this user can write there)</summary>
  static void Post(string message)
  {
    string inbox = Path.Combine(Folder(), "inbox");
    Directory.CreateDirectory(inbox);
    string name = DateTime.UtcNow.Ticks.ToString("D20") + "-" + Guid.NewGuid().ToString("N");
    string temp = Path.Combine(inbox, name + ".tmp");
    File.WriteAllText(temp, message, new UTF8Encoding(false));
    File.Move(temp, Path.Combine(inbox, name + ".msg"));
  }

  static void StartTray()
  {
    string folder = Folder();
    string q = folder.Replace("'", "''");
    string command =
      "$SlurpHelperDir = '" + q + "'; " +
      "try { iex ([IO.File]::ReadAllText($SlurpHelperDir + '\\slurp-helper.ps1', [Text.Encoding]::UTF8)) } " +
      "catch { $m = ($_ | Out-String); [IO.File]::WriteAllText($SlurpHelperDir + '\\error.log', $m); " +
      "Add-Type -AssemblyName System.Windows.Forms; [void][Windows.Forms.MessageBox]::Show($m, 'slurp helper: error') }";
    string encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(command));
    ProcessStartInfo si = new ProcessStartInfo();
    si.FileName = Path.Combine(Environment.SystemDirectory, "WindowsPowerShell\\v1.0\\powershell.exe");
    si.Arguments = "-NoProfile -STA -ExecutionPolicy Bypass -EncodedCommand " + encoded;
    si.UseShellExecute = false;
    si.CreateNoWindow = true;
    si.WorkingDirectory = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile); // (not the folder: Uninstall removes it)
    Process.Start(si);
  }

  static INPUT_RECORD Key(char c, ushort vk, bool down)
  {
    INPUT_RECORD r = new INPUT_RECORD();
    r.EventType = 1; // KEY_EVENT
    r.KeyEvent.KeyDown = down ? 1 : 0;
    r.KeyEvent.RepeatCount = 1;
    r.KeyEvent.VirtualKeyCode = vk;
    r.KeyEvent.UnicodeChar = (ushort)c;
    return r;
  }

  /// <summary>The line of the cursor and the one above it (a prompt may wrap); null if the screen cannot be read</summary>
  static string CursorLines(IntPtr output, int above = 1)
  {
    SCREEN_INFO info;
    if (!GetConsoleScreenBufferInfo(output, out info) || info.SizeX <= 0) return null;
    int top = Math.Max(0, info.CursorY - above);
    int count = info.SizeX * (info.CursorY - top + 1);
    char[] buffer = new char[count];
    uint read;
    if (!ReadConsoleOutputCharacterW(output, buffer, (uint)count, (uint)(top << 16), out read)) return null;
    return new string(buffer, 0, (int)read);
  }

  /// <summary>Waits (up to wait seconds) until the console of process pid shows "password:" at its cursor, then types
  /// text and Enter. 0: done; 2: no such console; 3: no input; 4: not written; 5: no prompt in time; 6: screen unreadable</summary>
  public static int Feed(uint pid, string text, int wait)
  {
    FreeConsole();
    if (!AttachConsole(pid)) return 2;
    try
    {
      IntPtr output = CreateFileW("CONOUT$", 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
      if (output == IntPtr.Zero || output == new IntPtr(-1)) return 6;
      try
      {
        DateTime end = DateTime.UtcNow.AddSeconds(wait);
        bool readable = false;
        while (true)
        {
          string lines = CursorLines(output);
          if (lines != null) readable = true;
          if (lines != null && lines.IndexOf("password:", StringComparison.OrdinalIgnoreCase) >= 0) break;
          if (DateTime.UtcNow > end) return readable ? 5 : 6;
          Thread.Sleep(150);
        }
      }
      finally { CloseHandle(output); }
      Thread.Sleep(100);
      IntPtr input = CreateFileW("CONIN$", 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero); // read|write, share r|w, open existing
      if (input == IntPtr.Zero || input == new IntPtr(-1)) return 3;
      try
      {
        INPUT_RECORD[] records = new INPUT_RECORD[(text.Length + 1) * 2];
        int i = 0;
        foreach (char c in text)
        {
          ushort vk = (ushort)(VkKeyScanW(c) & 0xff);
          records[i++] = Key(c, vk, true);
          records[i++] = Key(c, vk, false);
        }
        records[i++] = Key('\r', 0x0D, true);
        records[i++] = Key('\r', 0x0D, false);
        uint written;
        if (!WriteConsoleInputW(input, records, (uint)records.Length, out written) || written != records.Length) return 4;
        return 0;
      }
      finally { CloseHandle(input); }
    }
    finally { FreeConsole(); }
  }

  /// <summary>Waits (up to wait seconds) until the user logged in in the console of process pid: it showed a password
  /// prompt, then a line at the cursor that is not one (nor "Permission denied") for a second, or nothing for 5 s
  /// (a full screen program). 0: logged in; 2: no such console; 5: not in time; 6: screen unreadable</summary>
  public static int Watch(uint pid, int wait)
  {
    FreeConsole();
    if (!AttachConsole(pid)) return 2;
    try
    {
      IntPtr output = CreateFileW("CONOUT$", 0xC0000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero);
      if (output == IntPtr.Zero || output == new IntPtr(-1)) return 6;
      try
      {
        DateTime end = DateTime.UtcNow.AddSeconds(wait);
        bool readable = false, prompted = false;
        DateTime? other = null, blank = null;
        string last = null;
        while (DateTime.UtcNow < end)
        {
          string lines = CursorLines(output);
          string line = CursorLines(output, 0);
          if (lines != null && line != null)
          {
            readable = true;
            line = line.Trim();
            bool asks = line.IndexOf("password:", StringComparison.OrdinalIgnoreCase) >= 0;
            bool denied = lines.IndexOf("Permission denied", StringComparison.OrdinalIgnoreCase) >= 0;
            if (asks) { prompted = true; other = null; blank = null; }
            else if (prompted && line != "" && !denied)
            {
              if (line != last || other == null) other = DateTime.UtcNow;
              blank = null;
              if (DateTime.UtcNow - other.Value > TimeSpan.FromSeconds(1)) return 0;
            }
            else if (prompted && line == "" && !denied)
            {
              other = null;
              if (blank == null) blank = DateTime.UtcNow;
              if (DateTime.UtcNow - blank.Value > TimeSpan.FromSeconds(5)) return 0;
            }
            else { other = null; blank = null; }
            last = line;
          }
          Thread.Sleep(200);
        }
        return readable ? 5 : 6;
      }
      finally { CloseHandle(output); }
    }
    finally { FreeConsole(); }
  }

  /// <summary>For the tray: shows a window for real (whatever state Windows gave the process) and brings it forward</summary>
  public static void Bring(IntPtr hwnd)
  {
    ShowWindow(hwnd, 5);  // SW_SHOW
    ShowWindow(hwnd, 9);  // SW_RESTORE
    SetForegroundWindow(hwnd);
  }
}

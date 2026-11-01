#define WIN32_LEAN_AND_MEAN
#include <windows.h>

static wchar_t root[32768];
static wchar_t python[32768];
static wchar_t command[65536];
static STARTUPINFOW startup;
static PROCESS_INFORMATION child;

extern "C" void WINAPI WinMainCRTStartup() {
  DWORD count = GetModuleFileNameW(nullptr, root, 32768);
  if (!count || count >= 32768) ExitProcess(1);
  while (count && root[count - 1] != L'\\') --count;
  if (!count) ExitProcess(1);
  root[count - 1] = 0;
  // CreateProcess limits its complete command line to 32,767 characters.
  if (count > 16000) ExitProcess(1);
  // The launcher lives beside its private runtime; it never searches PATH.
  lstrcpyW(python, root);
  lstrcatW(python, L"\\runtime\\pythonw.exe");
  lstrcpyW(command, L"\"");
  lstrcatW(command, python);
  lstrcatW(command, L"\" -I \"");
  lstrcatW(command, root);
  lstrcatW(command, L"\\launch_env_window.pyw\"");
  startup.cb = sizeof(startup);
  if (!CreateProcessW(python, command, nullptr, nullptr, FALSE, CREATE_NO_WINDOW,
                       nullptr, root, &startup, &child)) {
    MessageBoxW(nullptr, L"启动组件缺失，请重新安装蓝豆指纹浏览器。",
                L"蓝豆指纹浏览器", MB_OK | MB_ICONERROR);
    ExitProcess(1);
  }
  CloseHandle(child.hThread);
  CloseHandle(child.hProcess);
  ExitProcess(0);
}

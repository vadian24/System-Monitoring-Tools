/*
 * Native x64 bootstrap. Stages the monitor payload and invokes Windows
 * PowerShell with the embedded setup command.
 */
#define WINIMPORT __declspec(dllimport)
typedef void *Handle;
typedef void *Module;
typedef unsigned long Dword;
typedef int Bool;
typedef unsigned short Wide;
typedef unsigned char Byte;

typedef struct {
    Dword size;
    Wide *reserved;
    Wide *desktop;
    Wide *title;
    Dword x, y, width, height, x_chars, y_chars, fill, flags;
    unsigned short show, reserved_size;
    Byte *reserved_data;
    Handle input, output, error;
} StartupInfo;

typedef struct {
    Handle process, thread;
    Dword process_id, thread_id;
} ProcessInfo;

WINIMPORT Module GetModuleHandleW(const Wide *);
WINIMPORT Handle FindResourceW(Module, const Wide *, const Wide *);
WINIMPORT Handle LoadResource(Module, Handle);
WINIMPORT void *LockResource(Handle);
WINIMPORT Dword SizeofResource(Module, Handle);
WINIMPORT Dword GetTempPathW(Dword, Wide *);
WINIMPORT unsigned GetTempFileNameW(const Wide *, const Wide *, unsigned, Wide *);
WINIMPORT unsigned GetSystemDirectoryW(Wide *, unsigned);
WINIMPORT Bool DeleteFileW(const Wide *);
WINIMPORT Handle CreateFileW(const Wide *, Dword, Dword, void *, Dword, Dword, Handle);
WINIMPORT Bool WriteFile(Handle, const void *, Dword, Dword *, void *);
WINIMPORT Bool CloseHandle(Handle);
WINIMPORT Bool SetEnvironmentVariableW(const Wide *, const Wide *);
WINIMPORT Bool CreateProcessW(const Wide *, Wide *, void *, void *, Bool, Dword, void *, const Wide *, StartupInfo *, ProcessInfo *);
WINIMPORT Dword WaitForSingleObject(Handle, Dword);
WINIMPORT Bool GetExitCodeProcess(Handle, Dword *);
WINIMPORT int MessageBoxW(Handle, const Wide *, const Wide *, unsigned);
WINIMPORT void ExitProcess(Dword);

#define ID(value) ((const Wide *)(unsigned long long)(value))
#define INVALID_HANDLE ((Handle)-1)
#define MAX_PATH 260
static Wide directory[MAX_PATH], payload_path[MAX_PATH], powershell[MAX_PATH];
static Wide command_line[32767];
static StartupInfo startup;
static ProcessInfo child;

static Dword append(Wide *target, Dword offset, Dword capacity, const Wide *source) {
    if (offset >= capacity) return capacity;
    for (; *source; ++source) {
        if (offset + 1 >= capacity) return capacity;
        target[offset++] = *source;
    }
    target[offset] = 0;
    return offset;
}

static const void *resource(unsigned id, Dword *length) {
    Module app = GetModuleHandleW(0);
    Handle item = FindResourceW(app, ID(id), ID(10));
    if (!item) return 0;
    *length = SizeofResource(app, item);
    Handle data = LoadResource(app, item);
    return data && *length ? LockResource(data) : 0;
}

static void fail(const Wide *message) {
    if (*payload_path) DeleteFileW(payload_path);
    MessageBoxW(0, message, L"Windows System Monitor Setup", 0x10);
    ExitProcess(1);
}

void setup_entry(void) {
    Dword zip_bytes = 0, script_bytes = 0, written = 0, exit_code = 1;
    const void *zip = resource(101, &zip_bytes);
    const Wide *script = resource(102, &script_bytes);
    if (!zip || !script || script_bytes < 2 || script_bytes % 2 ||
        script[script_bytes / 2 - 1] != 0) {
        fail(L"The installer payload is missing or damaged.");
    }
    Dword temp_length = GetTempPathW(MAX_PATH, directory);
    if (!temp_length || temp_length >= MAX_PATH ||
        !GetTempFileNameW(directory, L"SMW", 0, payload_path)) {
        fail(L"Setup could not create a temporary file.");
    }
    Handle file = CreateFileW(payload_path, 0x40000000, 0, 0, 2, 0x80, 0);
    if (file == INVALID_HANDLE) fail(L"Setup could not open its temporary file.");
    Bool saved = WriteFile(file, zip, zip_bytes, &written, 0);
    CloseHandle(file);
    if (!saved || written != zip_bytes) fail(L"Setup could not write its application files.");
    if (!SetEnvironmentVariableW(L"SYSTEM_MONITOR_SETUP_PAYLOAD", payload_path)) {
        fail(L"Setup could not pass its application files to PowerShell.");
    }
    Dword system_length = GetSystemDirectoryW(powershell, MAX_PATH);
    if (!system_length || system_length >= MAX_PATH ||
        append(powershell, system_length, MAX_PATH, L"\\WindowsPowerShell\\v1.0\\powershell.exe") >= MAX_PATH) {
        fail(L"Setup could not locate Windows PowerShell.");
    }
    Dword offset = append(command_line, 0, 32767, L"\"");
    offset = append(command_line, offset, 32767, powershell);
    offset = append(command_line, offset, 32767, L"\" -NoLogo -NoProfile -EncodedCommand ");
    if (append(command_line, offset, 32767, script) >= 32767) {
        fail(L"The embedded setup command is too large.");
    }
    startup.size = sizeof(startup);
    if (!CreateProcessW(powershell, command_line, 0, 0, 0, 0, 0, 0, &startup, &child)) {
        fail(L"Windows could not start PowerShell to complete setup.");
    }
    CloseHandle(child.thread);
    if (WaitForSingleObject(child.process, 0xffffffff) == 0) {
        GetExitCodeProcess(child.process, &exit_code);
    }
    CloseHandle(child.process);
    DeleteFileW(payload_path);
    ExitProcess(exit_code);
}

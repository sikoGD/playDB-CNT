/* playDB-CNT online shim for emulators without the Duskbloods online switches (stock Lichii, Eden).
 *
 * Built as version.dll and placed next to the emulator, it is loaded by the emulator at start
 * and forwards every real version.dll function to the Windows copy. When the launcher starts the
 * game it sets the same variables Duskbloods builds read, and the shim does the same as them:
 *
 *   EDEN_NX2_ONLINE_HOSTS=a.com,b.net  these names (and subdomains) resolve to 127.0.0.1, and the
 *                                      emulator's own block of Nintendo names is lifted for them
 *   EDEN_NX2_ONLINE_SERVER=ip:port     TCP connects to 127.0.0.1:443 go here instead
 *   EDEN_NX2_LOOPBACK_ONLY=1           other names fail to resolve; non-loopback traffic refused
 *   EDEN_NX2_CRASH_DUMP=<folder>       where it writes online_shim.log
 *
 * Without EDEN_NX2_ONLINE_HOSTS it changes nothing. It hooks getaddrinfo, gethostbyname, connect
 * and sendto in the emulator program's import table only (no other module, no other process). */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <ctype.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* --- forwarding of the real version.dll ------------------------------------------------------ */

typedef UINT_PTR (WINAPI *any_fn)(UINT_PTR, UINT_PTR, UINT_PTR, UINT_PTR, UINT_PTR, UINT_PTR, UINT_PTR, UINT_PTR);
static HMODULE real_version;

static any_fn real_fn(const char *name) {
    if (!real_version) {
        wchar_t path[MAX_PATH];
        UINT n = GetSystemDirectoryW(path, MAX_PATH);
        if (n == 0 || n > MAX_PATH - 20) return NULL;
        wcscat_s(path, MAX_PATH, L"\\version.dll");
        real_version = LoadLibraryW(path);
        if (!real_version) return NULL;
    }
    return (any_fn)GetProcAddress(real_version, name);
}

/* Every version.dll function takes at most 8 integer or pointer arguments and returns one: on x64
 * passing on 8 is safe whatever the real count. */
#define FORWARD(name)                                                                              \
    UINT_PTR WINAPI shim_##name(UINT_PTR a, UINT_PTR b, UINT_PTR c, UINT_PTR d, UINT_PTR e,         \
                                UINT_PTR f, UINT_PTR g, UINT_PTR h) {                               \
        static any_fn fn;                                                                          \
        if (!fn) fn = real_fn(#name);                                                              \
        if (!fn) { SetLastError(ERROR_PROC_NOT_FOUND); return 0; }                                  \
        return fn(a, b, c, d, e, f, g, h);                                                         \
    }
FORWARD(GetFileVersionInfoA)
FORWARD(GetFileVersionInfoByHandle)
FORWARD(GetFileVersionInfoExA)
FORWARD(GetFileVersionInfoExW)
FORWARD(GetFileVersionInfoSizeA)
FORWARD(GetFileVersionInfoSizeExA)
FORWARD(GetFileVersionInfoSizeExW)
FORWARD(GetFileVersionInfoSizeW)
FORWARD(GetFileVersionInfoW)
FORWARD(VerFindFileA)
FORWARD(VerFindFileW)
FORWARD(VerInstallFileA)
FORWARD(VerInstallFileW)
FORWARD(VerLanguageNameA)
FORWARD(VerLanguageNameW)
FORWARD(VerQueryValueA)
FORWARD(VerQueryValueW)

/* --- settings and log ------------------------------------------------------------------------ */

#define MAX_HOSTS 32
static char hosts[MAX_HOSTS][128];
static int host_count;
static struct sockaddr_in server;
static int has_server, loopback_only;
static FILE *log_file;
static CRITICAL_SECTION log_lock;

static void say(const char *format, ...) {
    if (!log_file) return;
    va_list args;
    va_start(args, format);
    EnterCriticalSection(&log_lock);
    SYSTEMTIME t;
    GetLocalTime(&t);
    fprintf(log_file, "%02u:%02u:%02u.%03u ", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds);
    vfprintf(log_file, format, args);
    fputc('\n', log_file);
    fflush(log_file);
    LeaveCriticalSection(&log_lock);
    va_end(args);
}

static int env(const char *name, char *out, DWORD size) {
    DWORD n = GetEnvironmentVariableA(name, out, size);
    return n > 0 && n < size;
}

static int read_settings(void) {
    char text[2048];
    if (!env("EDEN_NX2_ONLINE_HOSTS", text, sizeof text)) return 0;
    char *context = NULL;
    for (char *name = strtok_s(text, ",", &context); name && host_count < MAX_HOSTS;
         name = strtok_s(NULL, ",", &context)) {
        while (*name == ' ') name++;
        if (*name && strlen(name) < sizeof hosts[0]) {
            strcpy_s(hosts[host_count], sizeof hosts[0], name);
            _strlwr_s(hosts[host_count], sizeof hosts[0]);
            host_count++;
        }
    }
    if (env("EDEN_NX2_ONLINE_SERVER", text, sizeof text)) {
        unsigned a, b, c, d, port;
        if (sscanf_s(text, "%u.%u.%u.%u:%u", &a, &b, &c, &d, &port) == 5 && a < 256 && b < 256 &&
            c < 256 && d < 256 && port < 65536) {
            server.sin_family = AF_INET;
            server.sin_addr.s_addr = htonl((a << 24) | (b << 16) | (c << 8) | d);
            server.sin_port = htons((u_short)port);
            has_server = 1;
        }
    }
    if (env("EDEN_NX2_LOOPBACK_ONLY", text, sizeof text)) loopback_only = text[0] && text[0] != '0';
    if (env("EDEN_NX2_CRASH_DUMP", text, sizeof text) && strlen(text) < sizeof text - 20) {
        strcat_s(text, sizeof text, "\\online_shim.log");
        fopen_s(&log_file, text, "w");
    }
    return host_count > 0;
}

static int matches(const char *host) {
    if (!host) return 0;
    char name[256];
    size_t n = strlen(host);
    if (n == 0 || n >= sizeof name) return 0;
    for (size_t i = 0; i <= n; i++) name[i] = (char)tolower((unsigned char)host[i]);
    while (n && name[n - 1] == '.') name[--n] = 0;
    for (int i = 0; i < host_count; i++) {
        size_t m = strlen(hosts[i]);
        if (n == m && memcmp(name, hosts[i], m) == 0) return 1;
        if (n > m && memcmp(name + n - m, hosts[i], m) == 0 && name[n - m - 1] == '.') return 1;
    }
    return 0;
}

static int is_loopback(const struct sockaddr *addr) {
    return addr && addr->sa_family == AF_INET &&
           (ntohl(((const struct sockaddr_in *)addr)->sin_addr.s_addr) >> 24) == 127;
}

/* --- hooks ------------------------------------------------------------------------------------ */

typedef int (WSAAPI *getaddrinfo_fn)(PCSTR, PCSTR, const ADDRINFOA *, PADDRINFOA *);
typedef struct hostent *(WSAAPI *gethostbyname_fn)(const char *);
typedef int (WSAAPI *connect_fn)(SOCKET, const struct sockaddr *, int);
typedef int (WSAAPI *sendto_fn)(SOCKET, const char *, int, int, const struct sockaddr *, int);
static getaddrinfo_fn real_getaddrinfo;
static gethostbyname_fn real_gethostbyname;
static connect_fn real_connect;
static sendto_fn real_sendto;

static int WSAAPI hook_getaddrinfo(PCSTR node, PCSTR service, const ADDRINFOA *hints, PADDRINFOA *result) {
    if (matches(node)) {
        say("resolve %s -> 127.0.0.1", node);
        return real_getaddrinfo("127.0.0.1", service, hints, result);
    }
    int error = real_getaddrinfo(node, service, hints, result);
    if (error == 0 && loopback_only) {
        for (ADDRINFOA *item = *result; item; item = item->ai_next) {
            if (!is_loopback(item->ai_addr)) {
                freeaddrinfo(*result);
                *result = NULL;
                say("blocked lookup of %s (loopback only)", node ? node : "(null)");
                WSASetLastError(WSAHOST_NOT_FOUND);
                return WSAHOST_NOT_FOUND;
            }
        }
    }
    return error;
}

static struct hostent *WSAAPI hook_gethostbyname(const char *name) {
    if (matches(name)) {
        say("resolve %s -> 127.0.0.1 (gethostbyname)", name);
        return real_gethostbyname("127.0.0.1");
    }
    struct hostent *entry = real_gethostbyname(name);
    if (entry && loopback_only && entry->h_addrtype == AF_INET) {
        for (char **address = entry->h_addr_list; *address; address++) {
            if ((unsigned char)(*address)[0] != 127) {
                say("blocked lookup of %s (loopback only)", name ? name : "(null)");
                WSASetLastError(WSAHOST_NOT_FOUND);
                return NULL;
            }
        }
    }
    return entry;
}

static int WSAAPI hook_connect(SOCKET s, const struct sockaddr *name, int length) {
    struct sockaddr_in moved;
    if (has_server && name && name->sa_family == AF_INET && length >= (int)sizeof moved) {
        const struct sockaddr_in *in = (const struct sockaddr_in *)name;
        if (in->sin_addr.s_addr == htonl(INADDR_LOOPBACK) && in->sin_port == htons(443)) {
            moved = server;
            say("connect 127.0.0.1:443 -> %u.%u.%u.%u:%u", server.sin_addr.S_un.S_un_b.s_b1,
                server.sin_addr.S_un.S_un_b.s_b2, server.sin_addr.S_un.S_un_b.s_b3,
                server.sin_addr.S_un.S_un_b.s_b4, ntohs(server.sin_port));
            name = (const struct sockaddr *)&moved;
            length = sizeof moved;
        }
    }
    if (loopback_only && name && name->sa_family == AF_INET && !is_loopback(name)) {
        say("refused connect to a non-loopback address (loopback only)");
        WSASetLastError(WSAECONNREFUSED);
        return SOCKET_ERROR;
    }
    return real_connect(s, name, length);
}

static int WSAAPI hook_sendto(SOCKET s, const char *buffer, int length, int flags, const struct sockaddr *to,
                              int to_length) {
    if (loopback_only && to && to->sa_family == AF_INET && !is_loopback(to)) {
        WSASetLastError(WSAENETUNREACH);
        return SOCKET_ERROR;
    }
    return real_sendto(s, buffer, length, flags, to, to_length);
}

/* --- patching the emulator program ----------------------------------------------------------- */

static int write_memory(void *target, const void *data, size_t size) {
    DWORD old;
    if (!VirtualProtect(target, size, PAGE_READWRITE, &old)) return 0;
    memcpy(target, data, size);
    VirtualProtect(target, size, old, &old);
    return 1;
}

/* Point the program's imports of `function` from ws2_32/wsock32 at `hook`; returns the original. */
static void *hook_import(BYTE *base, const char *function, WORD ordinal, void *hook) {
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
    IMAGE_DATA_DIRECTORY dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    void *original = NULL;
    if (!dir.VirtualAddress) return NULL;
    for (IMAGE_IMPORT_DESCRIPTOR *d = (IMAGE_IMPORT_DESCRIPTOR *)(base + dir.VirtualAddress); d->Name; d++) {
        const char *dll = (const char *)(base + d->Name);
        if (_stricmp(dll, "ws2_32.dll") != 0 && _stricmp(dll, "wsock32.dll") != 0) continue;
        IMAGE_THUNK_DATA *names = (IMAGE_THUNK_DATA *)(base + (d->OriginalFirstThunk ? d->OriginalFirstThunk : d->FirstThunk));
        IMAGE_THUNK_DATA *slots = (IMAGE_THUNK_DATA *)(base + d->FirstThunk);
        for (; names->u1.AddressOfData; names++, slots++) {
            int hit;
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal))
                hit = ordinal && IMAGE_ORDINAL(names->u1.Ordinal) == ordinal;
            else
                hit = strcmp((const char *)((IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData))->Name, function) == 0;
            if (!hit) continue;
            if (!original) original = (void *)slots->u1.Function;
            ULONG_PTR value = (ULONG_PTR)hook;
            write_memory(&slots->u1.Function, &value, sizeof value);
        }
    }
    return original;
}

/* The emulator refuses to resolve Nintendo names (it answers "try again"); lift that for the names
 * the launcher serves by blanking those entries of its block list (whole strings only). */
/* The emulator blocks a name when it contains a block-list entry: lift each entry that one of the
 * launcher's names contains (srv.nintendo.net for npln.srv.nintendo.net, ...). */
static int blocks_ours(const char *entry) {
    for (int i = 0; i < host_count; i++)
        if (strstr(hosts[i], entry)) return 1;
    return 0;
}

static void unblock_names(BYTE *base) {
    static const char *const names[] = {"srv.nintendo.net", "nintendo.net", "npln.nintendo.net",
                                        "fromsoftware-game.net", "nintendo.com"};
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
    IMAGE_SECTION_HEADER *section = IMAGE_FIRST_SECTION(nt);
    for (WORD i = 0; i < nt->FileHeader.NumberOfSections; i++, section++) {
        if (memcmp(section->Name, ".rdata", 6) != 0) continue;
        BYTE *start = base + section->VirtualAddress;
        size_t size = section->Misc.VirtualSize;
        for (size_t k = 0; k < sizeof names / sizeof names[0]; k++) {
            if (!blocks_ours(names[k])) continue;
            size_t n = strlen(names[k]) + 1;  /* with its terminating NUL */
            for (size_t at = 1; at + n <= size; at++) {
                if (start[at - 1] == 0 && memcmp(start + at, names[k], n) == 0) {
                    BYTE never = 0x01;  /* no host name contains byte 1: the entry never matches */
                    write_memory(start + at, &never, 1);
                    say("lifted the emulator's block of %s", names[k]);
                }
            }
        }
    }
}

static void install(void) {
    if (!read_settings()) return;
    InitializeCriticalSection(&log_lock);
    BYTE *base = (BYTE *)GetModuleHandleW(NULL);
    say("playDB-CNT online shim: %d names, server %s, loopback only %d", host_count,
        has_server ? "set" : "none", loopback_only);
    HMODULE ws2 = LoadLibraryW(L"ws2_32.dll");
    real_getaddrinfo = (getaddrinfo_fn)GetProcAddress(ws2, "getaddrinfo");
    real_gethostbyname = (gethostbyname_fn)GetProcAddress(ws2, "gethostbyname");
    real_connect = (connect_fn)GetProcAddress(ws2, "connect");
    real_sendto = (sendto_fn)GetProcAddress(ws2, "sendto");
    if (!real_getaddrinfo || !real_gethostbyname || !real_connect || !real_sendto) {
        say("ws2_32 functions missing; nothing changed");
        return;
    }
    /* ws2_32 ordinals: connect 4, gethostbyname 52, sendto 20 (getaddrinfo has no fixed one). */
    int hooked = 0;
    hooked += hook_import(base, "getaddrinfo", 0, (void *)hook_getaddrinfo) != NULL;
    hooked += hook_import(base, "gethostbyname", 52, (void *)hook_gethostbyname) != NULL;
    hooked += hook_import(base, "connect", 4, (void *)hook_connect) != NULL;
    hooked += hook_import(base, "sendto", 20, (void *)hook_sendto) != NULL;
    say("hooked %d of 4 network functions", hooked);
    unblock_names(base);
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        install();
    }
    return TRUE;
}

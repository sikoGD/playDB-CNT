// stdafx.h : includefile for standard system include files,
// or project specific include files that are used frequently, but
// are changed infrequently
//

#pragma once

#define _CRT_SECURE_NO_WARNINGS 1

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

#if defined(_MSC_VER)
#include "targetver.h"
#include <tchar.h>
#include <intrin.h>
#include <Windows.h>
#pragma warning (disable: 4244)
#else
// playDB-CNT: GCC/Clang on macOS and Linux. x86-64 uses SSE directly; ARM (Apple Silicon,
// aarch64 Linux) maps the SSE intrinsics to NEON with sse2neon.h (MIT, see sse2neon-LICENSE).
#include <stdint.h>
#if defined(__aarch64__) || defined(__arm64__) || defined(__arm__)
#include "sse2neon.h"
#else
#include <x86intrin.h>
#endif
#define __forceinline inline __attribute__((always_inline))
static inline unsigned char _BitScanReverse(unsigned long *index, unsigned long mask) {
  if (!mask) return 0;
  *index = 31 - __builtin_clz((unsigned int)mask);
  return 1;
}
static inline unsigned char _BitScanForward(unsigned long *index, unsigned long mask) {
  if (!mask) return 0;
  *index = __builtin_ctz((unsigned int)mask);
  return 1;
}
#define _byteswap_ulong(x) __builtin_bswap32((uint32_t)(x))
#define _byteswap_uint64(x) __builtin_bswap64((uint64_t)(x))
#define _byteswap_ushort(x) __builtin_bswap16((uint16_t)(x))
#if !defined(_rotl)
static inline unsigned int _rotl(unsigned int v, int s) {
  s &= 31;
  return s ? (v << s) | (v >> (32 - s)) : v;
}
#endif
#endif

// TODO: reference additional headers your program requires here
typedef unsigned char byte;
typedef unsigned char uint8;
typedef unsigned int uint32;
typedef unsigned long long uint64;
typedef signed long long int64;
typedef signed int int32;
typedef unsigned short uint16;
typedef signed short int16;
typedef unsigned int uint;

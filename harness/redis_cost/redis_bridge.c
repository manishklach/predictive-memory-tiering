/* Disposable benchmark module for Redis 7.0.15. Unix socket only. */
#include "redismodule.h"
#include <Python.h>
#include <dlfcn.h>
#include <stdint.h>
#include <string.h>

#define PAGE_BYTES 4096
#ifndef PMT_REDIS_PAGES
#define PMT_REDIS_PAGES 8192
#endif
_Static_assert(PMT_REDIS_PAGES > 0 && PMT_REDIS_PAGES <= 65536,
               "Redis fixture must fit within 256 MiB plus alignment padding");

static RedisModuleCtx *active;
static size_t displacement;

/* Called only by the embedded driver on the server command thread. */
int pmt_getrange(int page)
{
	if (!active || page < 0 || page >= PMT_REDIS_PAGES)
		return -1;
	long long begin = displacement + page * (long long)PAGE_BYTES;
	RedisModuleCallReply *reply = RedisModule_Call(active, "GETRANGE", "cll",
						      "pmt:blob", begin, begin + 255);
	if (!reply)
		return -1;
	size_t len = 0;
	const char *s = RedisModule_CallReplyStringPtr(reply, &len);
	int value = s && len == 256 ? (unsigned char)s[0] : -1;
	RedisModule_FreeCallReply(reply);
	return value;
}

static int bench(RedisModuleCtx *ctx, RedisModuleString **argv, int argc)
{
	if (argc != 1)
		return RedisModule_WrongArity(ctx);
	RedisModule_AutoMemory(ctx);
	RedisModuleString *name = RedisModule_CreateString(ctx, "pmt:blob", 8);
	RedisModuleKey *key = RedisModule_OpenKey(ctx, name, REDISMODULE_READ | REDISMODULE_WRITE);
	if (RedisModule_KeyType(key) != REDISMODULE_KEYTYPE_EMPTY)
		return RedisModule_ReplyWithError(ctx, "benchmark requires an empty fixture");
	if (RedisModule_StringTruncate(key, (PMT_REDIS_PAGES + 1) * PAGE_BYTES) != REDISMODULE_OK)
		return RedisModule_ReplyWithError(ctx, "string allocation failed");
	size_t length;
	char *base = RedisModule_StringDMA(key, &length, REDISMODULE_WRITE);
	if (!base || length != (PMT_REDIS_PAGES + 1) * PAGE_BYTES)
		return RedisModule_ReplyWithError(ctx, "invalid Redis value");
	displacement = (-(uintptr_t)base) & (PAGE_BYTES - 1);
	char *aligned = base + displacement;
	for (int i = 0; i < PMT_REDIS_PAGES; ++i)
		memset(aligned + i * PAGE_BYTES, i % 251, PAGE_BYTES);
	RedisModule_CloseKey(key);
	active = ctx;
	PyObject *driver = PyImport_ImportModule("redis_worker");
	PyObject *result = driver ? PyObject_CallMethod(driver, "run", "Ki",
					 (unsigned long long)(uintptr_t)aligned, PMT_REDIS_PAGES) : NULL;
	active = NULL;
	Py_XDECREF(driver);
	if (!result) {
		PyErr_Print();
		return RedisModule_ReplyWithError(ctx, "embedded benchmark failed");
	}
	Py_DECREF(result);
	return RedisModule_ReplyWithSimpleString(ctx, "OK");
}

int RedisModule_OnLoad(RedisModuleCtx *ctx, RedisModuleString **argv, int argc)
{
	if (RedisModule_Init(ctx, "pmtbench", 1, REDISMODULE_APIVER_1) == REDISMODULE_ERR)
		return REDISMODULE_ERR;
	if (!dlopen(PMT_LIBPYTHON, RTLD_NOW | RTLD_GLOBAL))
		return REDISMODULE_ERR;
	Py_Initialize();
	return RedisModule_CreateCommand(ctx, "pmtbench.run", bench, "write admin", 0, 0, 0);
}

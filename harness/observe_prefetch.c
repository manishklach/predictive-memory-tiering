/* Optional observer for a separate correctness run of the cooperative arena. */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

static bool task_read_bytes(uint64_t *result)
{
	FILE *file = fopen("/proc/self/io", "r");
	char line[128];
	bool found = false;

	if (!file)
		return false;
	while (fgets(line, sizeof(line), file)) {
		if (sscanf(line, "read_bytes: %" SCNu64, result) == 1) {
			found = true;
			break;
		}
	}
	fclose(file);
	return found;
}

int madvise(void *addr, size_t len, int advice)
{
	static bool recorded;
	unsigned char residency;
	uint64_t before, after;

	if (advice != MADV_WILLNEED || len != 4096 || recorded ||
	    mincore(addr, len, &residency) || (residency & 1) || !task_read_bytes(&before))
		return syscall(SYS_madvise, addr, len, advice);

	int rc = syscall(SYS_madvise, addr, len, advice);
	int saved_errno = errno;

	if (!rc && task_read_bytes(&after) && after >= before + len) {
		for (int i = 0; i < 50; i++) {
			if (mincore(addr, len, &residency))
				break;
			if (residency & 1) {
				fprintf(stderr, "{\"scope\":\"anonymous arena WILLNEED syscall\","
					"\"resident_before\":false,\"resident_before_demand\":true,"
					"\"task_read_bytes_delta\":%" PRIu64 "}\n", after - before);
				recorded = true;
				break;
			}
			usleep(1000);
		}
	}
	errno = saved_errno;
	return rc;
}

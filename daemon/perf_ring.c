#define _GNU_SOURCE
#include <errno.h>
#include <linux/perf_event.h>
#include <stdint.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/* One non-inherited event follows the dedicated kdamond across CPUs. */
int pmt_trace_open(unsigned int event, int tid)
{
	struct perf_event_attr attr = {
		.size = sizeof(attr),
		.type = PERF_TYPE_TRACEPOINT,
		.config = event,
		.sample_period = 1,
		.sample_type = PERF_SAMPLE_TIME | PERF_SAMPLE_RAW,
		.wakeup_events = 1,
		.use_clockid = 1,
		.clockid = CLOCK_MONOTONIC,
	};

	return syscall(SYS_perf_event_open, &attr, tid, -1, -1, PERF_FLAG_FD_CLOEXEC);
}

/* Snapshot complete records before releasing the ring space to the kernel. */
int pmt_trace_copy(struct perf_event_mmap_page *meta, unsigned char *dst,
		   unsigned int capacity)
{
	uint64_t head = __atomic_load_n(&meta->data_head, __ATOMIC_ACQUIRE);
	uint64_t tail = meta->data_tail;
	uint64_t count = head - tail;
	uint64_t size = meta->data_size;
	const unsigned char *ring = (unsigned char *)meta + meta->data_offset;
	uint64_t offset, first;

	if (!size || count > size || count > capacity)
		return -EOVERFLOW;
	offset = tail % size;
	first = count < size - offset ? count : size - offset;
	memcpy(dst, ring + offset, first);
	memcpy(dst + first, ring, count - first);
	__atomic_store_n(&meta->data_tail, head, __ATOMIC_RELEASE);
	return (int)count;
}

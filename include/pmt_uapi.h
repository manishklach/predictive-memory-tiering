/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef PMT_UAPI_H
#define PMT_UAPI_H

#include <linux/ioctl.h>
#include <linux/types.h>

#define PMT_TRACE_ABI 1
#define PMT_ACCESS_SYNTHETIC 0
#define PMT_TRACE_CAPACITY 256

/* One native-endian record per read. Synthetic page IDs are sequence numbers. */
struct pmt_event {
	__u64 timestamp_ns;
	__u64 page_id;
	__u32 pid;
	__u16 abi;
	__u16 access_type;
};

/* Read-only shared mapping. Load producer with acquire semantics before records. */
struct pmt_ring {
	__u32 abi;
	__u32 capacity;
	__u32 producer;
	__u32 consumer;
	struct pmt_event records[PMT_TRACE_CAPACITY];
};

/* Advance consumer only if it still matches the snapshot originally read. */
struct pmt_consume {
	__u32 expected;
	__u32 count;
};

#define PMT_TRACE_CONSUME _IOW('P', 0x01, struct pmt_consume)

#endif

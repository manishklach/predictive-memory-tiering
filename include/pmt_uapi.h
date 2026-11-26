/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef PMT_UAPI_H
#define PMT_UAPI_H

#include <linux/types.h>

#define PMT_TRACE_ABI 1
#define PMT_ACCESS_SYNTHETIC 0

/* One native-endian record per read. Synthetic page IDs are sequence numbers. */
struct pmt_event {
	__u64 timestamp_ns;
	__u64 page_id;
	__u32 pid;
	__u16 abi;
	__u16 access_type;
};

#endif

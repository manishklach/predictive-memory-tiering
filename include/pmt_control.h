#ifndef PMT_CONTROL_H
#define PMT_CONTROL_H

#include <stdint.h>

#define PMT_MAX_ARENA_PAGES 8192
#define PMT_NO_PAGE UINT32_MAX

/* Local protocol between a cooperative arena and its single controller. */
struct pmt_observation {
	uint64_t timestamp_ns;
	uint32_t page;
	uint32_t wrote;
};

/* Each observation is followed by one residency byte per arena page. */
struct pmt_advice {
	uint32_t evict;
	uint32_t prefetch;
};

enum pmt_model {
	PMT_MODEL_NONE,
	PMT_MODEL_STRIDE,
	PMT_MODEL_MARKOV,
};

int pmt_control(uint32_t pages, uint32_t budget, uint64_t idle_ns,
		uint64_t dwell_ns, enum pmt_model model);

#endif

#define _POSIX_C_SOURCE 200809L
#include <time.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#include "pmt_control.h"

#define PMT_TRANSITIONS 4096

struct page_history {
	uint64_t last;
	uint64_t candidate_since;
	uint64_t touches;
	double gap;
	double deviation;
	double frequency;
	bool candidate;
	bool dirty;
};

struct transition {
	uint32_t first;
	uint32_t second;
	uint32_t next;
	uint32_t votes;
};

struct policy {
	struct page_history *history;
	struct transition table[PMT_TRANSITIONS];
	uint32_t previous;
	uint32_t current;
	uint32_t pages;
	uint32_t budget;
	uint64_t idle_ns;
	uint64_t dwell_ns;
	enum pmt_model model;
};

static struct transition *entry(struct policy *policy, uint32_t first, uint32_t second)
{
	uint32_t hash = first * UINT32_C(0x9e3779b1) ^ second * UINT32_C(0x85ebca77);

	return &policy->table[(hash ^ (hash >> 16)) & (PMT_TRANSITIONS - 1)];
}

static uint32_t predict(struct policy *policy, uint32_t next)
{
	uint32_t previous = policy->previous, current = policy->current;
	struct transition *item;

	policy->previous = current;
	policy->current = next;
	if (current == PMT_NO_PAGE)
		return PMT_NO_PAGE;
	if (policy->model == PMT_MODEL_STRIDE) {
		int64_t candidate = 2 * (int64_t)next - current;

		if (candidate >= 0 && candidate < policy->pages && candidate != next)
			return candidate;
	}
	if (policy->model != PMT_MODEL_MARKOV)
		return PMT_NO_PAGE;
	if (previous != PMT_NO_PAGE) {
		item = entry(policy, previous, current);
		if (!item->votes || item->first != previous || item->second != current) {
			*item = (struct transition){previous, current, next, 1};
		} else if (item->next == next) {
			if (item->votes < 255)
				item->votes++;
		} else {
			item->votes--;
		}
	}
	item = entry(policy, current, next);
	if (item->votes && item->first == current && item->second == next)
		return item->next;
	return PMT_NO_PAGE;
}

static void observe(struct page_history *item, const struct pmt_observation *obs,
		    uint64_t idle_ns)
{
	if (item->touches) {
		double gap = obs->timestamp_ns - item->last;
		double delta = gap - item->gap;
		double deviation = delta < 0 ? -delta : delta;
		double decay = 1 + gap / idle_ns;

		item->gap = item->touches == 1 ? gap : item->gap + delta / 4;
		item->deviation = item->touches == 1 ? 0 :
			item->deviation + (deviation - item->deviation) / 4;
		item->frequency = item->frequency / decay + 1;
	} else {
		item->frequency = 1;
	}
	item->last = obs->timestamp_ns;
	item->touches++;
	item->dirty |= obs->wrote != 0;
	item->candidate = false;
}

static double cold_score(const struct page_history *item, uint64_t now, uint64_t idle_ns)
{
	double idle, frequency, score;

	idle = now - item->last;
	frequency = item->frequency / (1 + idle / idle_ns);
	score = idle / idle_ns - 0.25 * item->dirty -
		0.25 * (frequency / (1 + frequency));
	if (item->touches > 2 && item->deviation <= 0.25 * item->gap &&
	    idle <= 1.25 * item->gap)
		score -= 1;
	return score;
}

#ifdef PMT_PROFILE
size_t pmt_region_state_size(void)
{
	return sizeof(struct page_history);
}

/* One positive aggregate is one observed touch; a new cold range starts its
 * idle clock on discovery. No per-page dirty or residency bits are available. */
double pmt_region_sample(struct page_history *item, uint64_t now, uint32_t hits,
			 uint64_t idle_ns, uint64_t dwell_ns)
{
	double score;

	if (hits) {
		struct pmt_observation obs = {.timestamp_ns = now};

		observe(item, &obs, idle_ns);
		return 0;
	}
	if (!item->last)
		item->last = now;
	score = cold_score(item, now, idle_ns);
	if (score <= 1) {
		item->candidate = false;
		return 0;
	}
	if (!item->candidate) {
		item->candidate = true;
		item->candidate_since = now;
	}
	return now - item->candidate_since >= dwell_ns ? score : 0;
}
#endif

static struct pmt_advice decide(struct policy *policy, const struct pmt_observation *obs,
				const unsigned char *resident)
{
	struct pmt_advice advice = {PMT_NO_PAGE, PMT_NO_PAGE};
	uint32_t predicted = predict(policy, obs->page);
	uint32_t occupied = 0;
	bool prefetchable;
	double best = 1;

	observe(&policy->history[obs->page], obs, policy->idle_ns);
	for (uint32_t page = 0; page < policy->pages; page++) {
		struct page_history *item = &policy->history[page];
		double score;

		if (!resident[page]) {
			item->candidate = false;
			item->dirty = false;
			continue;
		}
		occupied++;
		if (!item->touches || page == obs->page || page == predicted) {
			item->candidate = false;
			continue;
		}
		score = cold_score(item, obs->timestamp_ns, policy->idle_ns);
		if (score <= 1) {
			item->candidate = false;
			continue;
		}
		if (!item->candidate) {
			item->candidate_since = obs->timestamp_ns;
			item->candidate = true;
		}
		if (score > best && obs->timestamp_ns - item->candidate_since >= policy->dwell_ns) {
			advice.evict = page;
			best = score;
		}
	}
	prefetchable = predicted != PMT_NO_PAGE && !resident[predicted] &&
		       policy->history[predicted].touches;
	if (occupied < policy->budget || (occupied == policy->budget && !prefetchable))
		advice.evict = PMT_NO_PAGE;
	else if (advice.evict != PMT_NO_PAGE)
		policy->history[advice.evict].candidate = false;
	if (prefetchable && (occupied < policy->budget ||
	    (occupied == policy->budget && advice.evict != PMT_NO_PAGE)))
		advice.prefetch = predicted;
	return advice;
}

#ifdef PMT_PROFILE
static uint64_t monotonic_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now))
		abort();
	return (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec;
}
#endif

struct policy *pmt_policy_create(uint32_t pages, uint32_t budget, uint64_t idle_ns,
                                 uint64_t dwell_ns, enum pmt_model model)
{
	struct policy *policy;

	if (!pages || pages > PMT_MAX_ARENA_PAGES || !budget || budget > pages || !idle_ns)
		return NULL;
	policy = calloc(1, sizeof(*policy));
	if (!policy)
		return NULL;
	policy->history = calloc(pages, sizeof(*policy->history));
	if (!policy->history) {
		free(policy);
		return NULL;
	}
	policy->pages = pages;
	policy->budget = budget;
	policy->idle_ns = idle_ns;
	policy->dwell_ns = dwell_ns;
	policy->model = model;
	policy->previous = PMT_NO_PAGE;
	policy->current = PMT_NO_PAGE;
	return policy;
}

void pmt_policy_destroy(struct policy *policy)
{
	if (policy)
		free(policy->history);
	free(policy);
}

#ifdef PMT_PROFILE
/* Detector-only SQLite runs retain touch history between ranking passes. */
void pmt_detector_observe(struct policy *policy, const struct pmt_observation *obs)
{
	observe(&policy->history[obs->page], obs, policy->idle_ns);
}

uint64_t pmt_profile_step(struct policy *policy, const struct pmt_observation *obs,
			  const unsigned char *resident, struct pmt_advice *advice)
{
	uint64_t start = monotonic_ns();

	*advice = decide(policy, obs, resident);
	return monotonic_ns() - start;
}
#endif

int pmt_control(uint32_t pages, uint32_t budget, uint64_t idle_ns,
		uint64_t dwell_ns, enum pmt_model model)
{
	struct policy *policy = pmt_policy_create(pages, budget, idle_ns, dwell_ns, model);
	unsigned char *resident = malloc(pages);
	uint64_t last_timestamp = 0;
	int status = EXIT_FAILURE;

	if (!policy || !resident) {
		perror("allocate controller");
		goto out;
	}
	for (;;) {
		struct pmt_observation obs;
		struct pmt_advice advice;
		size_t length = fread(&obs, 1, sizeof(obs), stdin);

		if (length == 0 && feof(stdin) && !ferror(stdin)) {
			status = EXIT_SUCCESS;
			break;
		}
		if (length != sizeof(obs) || obs.page >= pages || obs.wrote > 1 ||
		    obs.timestamp_ns < last_timestamp ||
		    fread(resident, 1, pages, stdin) != pages || !resident[obs.page]) {
			fputs("invalid arena observation\n", stderr);
			break;
		}
		last_timestamp = obs.timestamp_ns;
#ifdef PMT_PROFILE
		uint64_t score_ns = monotonic_ns();
#endif
		advice = decide(policy, &obs, resident);
#ifdef PMT_PROFILE
		score_ns = monotonic_ns() - score_ns;
#endif
		if (fwrite(&advice, sizeof(advice), 1, stdout) != 1 ||
#ifdef PMT_PROFILE
		    fwrite(&score_ns, sizeof(score_ns), 1, stdout) != 1 ||
#endif
		    fflush(stdout) == EOF) {
			perror("write advice");
			break;
		}
	}

out:
	pmt_policy_destroy(policy);
	free(resident);
	return status;
}

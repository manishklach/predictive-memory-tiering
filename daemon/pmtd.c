#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <sched.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#include "pmt_uapi.h"
#include "pmt_control.h"

_Static_assert(sizeof(struct pmt_event) == 24, "unexpected PMT record size");

static long number(const char *arg, long min, long max)
{
	char *end;
	long result;

	errno = 0;
	result = strtol(arg, &end, 10);
	if (errno || end == arg || *end || result < min || result > max) {
		fprintf(stderr, "invalid numeric argument: %s\n", arg);
		exit(EXIT_FAILURE);
	}
	return result;
}

static int wait_readable(int fd, int timeout)
{
	struct pollfd source = { .fd = fd, .events = POLLIN };
	int ready;

	do {
		ready = poll(&source, 1, timeout);
	} while (ready < 0 && errno == EINTR);
	if (ready <= 0) {
		if (!ready)
			errno = ETIMEDOUT;
		perror("poll trace");
		return -1;
	}
	return 0;
}

static int next_mapped(int fd, const struct pmt_ring *ring, struct pmt_event *event,
		       int timeout)
{
	for (;;) {
		struct pmt_consume cursor = {.count = 1};
		uint32_t available;

		if (wait_readable(fd, timeout))
			return -1;
		cursor.expected = __atomic_load_n(&ring->consumer, __ATOMIC_ACQUIRE);
		available = __atomic_load_n(&ring->producer, __ATOMIC_ACQUIRE) - cursor.expected;
		if (!available)
			continue;
		if (available > PMT_TRACE_CAPACITY) {
			fputs("invalid PMT ring indices\n", stderr);
			return -1;
		}
		*event = ring->records[cursor.expected % PMT_TRACE_CAPACITY];
		if (ioctl(fd, PMT_TRACE_CONSUME, &cursor) == 0)
			return 0;
		if (errno != EAGAIN && errno != EINTR) {
			perror("consume PMT trace");
			return -1;
		}
	}
}

static int next_event(int fd, const struct pmt_ring *ring, struct pmt_event *event,
		      int timeout)
{
	ssize_t bytes = sizeof(*event);

	if (ring) {
		if (next_mapped(fd, ring, event, timeout))
			return -1;
		goto validate;
	}
	if (wait_readable(fd, timeout))
		return -1;
	do {
		bytes = read(fd, event, sizeof(*event));
	} while (bytes < 0 && errno == EINTR);
	if (bytes < 0) {
		perror("read trace");
		return -1;
	}
validate:
	if (bytes != sizeof(*event) || event->abi != PMT_TRACE_ABI ||
	    event->access_type != PMT_ACCESS_SYNTHETIC) {
		fputs("invalid or unsupported PMT trace record\n", stderr);
		return -1;
	}
	return 0;
}

static int control_cli(int argc, char **argv)
{
	enum pmt_model model;
	long pages, budget, idle_ms, dwell_ms;

	if (argc != 7) {
		fputs("usage: pmtd --control PAGES BUDGET_PAGES IDLE_MS DWELL_MS none|stride|markov\n",
		      stderr);
		return EXIT_FAILURE;
	}
	pages = number(argv[2], 1, PMT_MAX_ARENA_PAGES);
	budget = number(argv[3], 1, pages);
	idle_ms = number(argv[4], 1, 3600000);
	dwell_ms = number(argv[5], 0, 3600000);
	if (!strcmp(argv[6], "none"))
		model = PMT_MODEL_NONE;
	else if (!strcmp(argv[6], "stride"))
		model = PMT_MODEL_STRIDE;
	else if (!strcmp(argv[6], "markov"))
		model = PMT_MODEL_MARKOV;
	else {
		fputs("unknown prediction model\n", stderr);
		return EXIT_FAILURE;
	}
	return pmt_control(pages, budget, (uint64_t)idle_ms * 1000000,
			   (uint64_t)dwell_ms * 1000000, model);
}

int main(int argc, char **argv)
{
	const char *path = "/dev/pmt_trace";
	const struct pmt_ring *ring = NULL;
	bool use_mmap = false;
	long count = 1;
	int timeout = 5000;
	int fd, result = EXIT_FAILURE, option;

	if (argc >= 2 && !strcmp(argv[1], "--control"))
		return control_cli(argc, argv);

	while ((option = getopt(argc, argv, "d:n:t:c:mh")) != -1) {
		switch (option) {
		case 'd':
			path = optarg;
			break;
		case 'n':
			count = number(optarg, 1, 10000000);
			break;
		case 't':
			timeout = number(optarg, 1, 600000);
			break;
		case 'm':
			use_mmap = true;
			break;
		case 'c': {
			cpu_set_t mask;
			long core = number(optarg, 0, CPU_SETSIZE - 1);

			CPU_ZERO(&mask);
			CPU_SET(core, &mask);
			if (sched_setaffinity(0, sizeof(mask), &mask) < 0) {
				perror("set CPU affinity");
				return EXIT_FAILURE;
			}
			break;
		}
		default:
			fprintf(stderr, "usage: %s [-m] [-d trace] [-n records] [-t timeout_ms] [-c cpu]\n",
				argv[0]);
			return option == 'h' ? EXIT_SUCCESS : EXIT_FAILURE;
		}
	}
	if (optind != argc) {
		fputs("unexpected positional argument\n", stderr);
		return EXIT_FAILURE;
	}
	fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
	if (fd < 0) {
		perror(path);
		return EXIT_FAILURE;
	}
	if (use_mmap) {
		void *mapping = mmap(NULL, sizeof(*ring), PROT_READ, MAP_SHARED, fd, 0);

		if (mapping == MAP_FAILED) {
			perror("map PMT ring");
			goto out;
		}
		ring = mapping;
		if (ring->abi != PMT_TRACE_ABI || ring->capacity != PMT_TRACE_CAPACITY) {
			fputs("invalid or unsupported PMT ring\n", stderr);
			goto out;
		}
	}
	for (long i = 0; i < count; i++) {
		struct pmt_event event;

		if (next_event(fd, ring, &event, timeout) < 0)
			goto out;
		printf("{\"timestamp_ns\":%" PRIu64 ",\"pid\":%" PRIu32
		       ",\"page_id\":%" PRIu64 ",\"access_type\":\"synthetic\"}\n",
		       (uint64_t)event.timestamp_ns, (uint32_t)event.pid,
		       (uint64_t)event.page_id);
	}
	if (fflush(stdout) == EOF || ferror(stdout)) {
		perror("write output");
		goto out;
	}
	result = EXIT_SUCCESS;
out:
	if (ring)
		munmap((void *)ring, sizeof(*ring));
	close(fd);
	return result;
}

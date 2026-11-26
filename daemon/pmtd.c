#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#include "pmt_uapi.h"

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

static int next_event(int fd, struct pmt_event *event, int timeout)
{
	struct pollfd source = { .fd = fd, .events = POLLIN };
	int ready;
	ssize_t bytes;

	do {
		ready = poll(&source, 1, timeout);
	} while (ready < 0 && errno == EINTR);
	if (ready <= 0) {
		if (!ready)
			errno = ETIMEDOUT;
		perror("poll trace");
		return -1;
	}
	do {
		bytes = read(fd, event, sizeof(*event));
	} while (bytes < 0 && errno == EINTR);
	if (bytes < 0) {
		perror("read trace");
		return -1;
	}
	if (bytes != sizeof(*event) || event->abi != PMT_TRACE_ABI ||
	    event->access_type != PMT_ACCESS_SYNTHETIC) {
		fputs("invalid or unsupported PMT trace record\n", stderr);
		return -1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	const char *path = "/dev/pmt_trace";
	long count = 1;
	int timeout = 5000;
	int fd, result = EXIT_FAILURE, option;

	while ((option = getopt(argc, argv, "d:n:t:c:h")) != -1) {
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
			fprintf(stderr, "usage: %s [-d trace] [-n records] [-t timeout_ms] [-c cpu]\n",
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
	for (long i = 0; i < count; i++) {
		struct pmt_event event;

		if (next_event(fd, &event, timeout) < 0)
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
	close(fd);
	return result;
}

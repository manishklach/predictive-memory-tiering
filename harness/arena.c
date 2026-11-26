#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/magic.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <sys/vfs.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "pmt_control.h"

static long number(const char *arg, long min, long max)
{
	char *end;
	long value;

	errno = 0;
	value = strtol(arg, &end, 10);
	if (errno || end == arg || *end || value < min || value > max) {
		fprintf(stderr, "invalid number: %s\n", arg);
		exit(EXIT_FAILURE);
	}
	return value;
}

static uint64_t monotonic_ns(void)
{
	struct timespec stamp;

	if (clock_gettime(CLOCK_MONOTONIC, &stamp)) {
		perror("clock_gettime");
		exit(EXIT_FAILURE);
	}
	return (uint64_t)stamp.tv_sec * 1000000000 + stamp.tv_nsec;
}

static int compare_latency(const void *left, const void *right)
{
	uint64_t x = *(const uint64_t *)left, y = *(const uint64_t *)right;

	return (x > y) - (x < y);
}

static int exchange(int fd, struct pmt_observation *obs, unsigned char *residency,
		    size_t pages, struct pmt_advice *advice)
{
	struct iovec iov[] = {{obs, sizeof(*obs)}, {residency, pages}};
	struct msghdr request = {.msg_iov = iov, .msg_iovlen = 2};
	struct pollfd pollfd = {.fd = fd, .events = POLLIN};
	unsigned char *reply = (unsigned char *)advice;
	uint64_t deadline = monotonic_ns() + 500000000;
	size_t read_bytes = 0;
	ssize_t sent = sendmsg(fd, &request, MSG_DONTWAIT | MSG_NOSIGNAL);

	if (sent != (ssize_t)(sizeof(*obs) + pages))
		return -1;
	while (read_bytes < sizeof(*advice)) {
		uint64_t now = monotonic_ns();
		ssize_t received;

		if (now >= deadline ||
		    poll(&pollfd, 1, (deadline - now + 999999) / 1000000) != 1)
			return -1;
		received = recv(fd, reply + read_bytes, sizeof(*advice) - read_bytes, MSG_DONTWAIT);
		if (received <= 0)
			return -1;
		read_bytes += received;
	}
	if ((advice->evict != PMT_NO_PAGE &&
	     (advice->evict >= pages || advice->evict == obs->page || !residency[advice->evict])) ||
	    (advice->prefetch != PMT_NO_PAGE &&
	     (advice->prefetch >= pages || residency[advice->prefetch])))
		return -1;
	return 0;
}

static void start_child(int pair[2], char **argv, long core)
{
	close(pair[0]);
	if (core >= 0) {
		cpu_set_t cpus;

		CPU_ZERO(&cpus);
		CPU_SET(core, &cpus);
		if (sched_setaffinity(0, sizeof(cpus), &cpus))
			goto failure;
	}
	if (dup2(pair[1], STDIN_FILENO) < 0 || dup2(pair[1], STDOUT_FILENO) < 0)
		goto failure;
	close(pair[1]);
	execl("./pmtd", "pmtd", "--control", argv[2], argv[3], argv[5], argv[6], argv[1],
	      (char *)NULL);
failure:
	perror("start pmtd");
	_exit(126);
}

static int join_workload_cgroup(const char *path)
{
	int directory = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	struct statfs info;
	int fd, result = -1;

	if (directory < 0)
		return -1;
	if (fstatfs(directory, &info))
		goto out;
	if (info.f_type != CGROUP2_SUPER_MAGIC) {
		errno = EINVAL;
		goto out;
	}
	fd = openat(directory, "cgroup.procs", O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd >= 0) {
		if (write(fd, "0\n", 2) == 2)
			result = 0;
		else if (!errno)
			errno = EIO;
		close(fd);
	}
out:
	close(directory);
	return result;
}

static int reap_controller(pid_t daemon, struct rusage *usage)
{
	int status;
	pid_t reaped;

	do {
		reaped = wait4(daemon, &status, 0, usage);
	} while (reaped < 0 && errno == EINTR);
	return reaped > 0 && WIFEXITED(status) && !WEXITSTATUS(status) ? 0 : -1;
}

static uint64_t time_us(struct timeval stamp)
{
	return (uint64_t)stamp.tv_sec * 1000000 + stamp.tv_usec;
}

int main(int argc, char **argv)
{
	int pair[2], connection = -1, status = EXIT_FAILURE;
	size_t pages, rounds, count, budget, max_resident = 0;
	uint64_t *latency = NULL, began, elapsed;
	size_t nonresident = 0, samples = 0, evict = 0, evict_observed = 0;
	size_t prefetch = 0, prefetch_resident = 0, prefetch_nonresident = 0;
	size_t prefetch_unused = 0, errors = 0;
	long core = -1, page_size, delay;
	unsigned char *residency = NULL, *expected = NULL, *requests = NULL;
	unsigned char *arena = MAP_FAILED;
	pid_t daemon = -1;
	bool stock, disconnected = false;
	const char *cgroup = getenv("PMT_WORKLOAD_CGROUP");
	struct rusage controller_usage = {0};

	if (argc < 8 || argc > 9 ||
	    (strcmp(argv[1], "stock") && strcmp(argv[1], "none") &&
	     strcmp(argv[1], "stride") && strcmp(argv[1], "markov"))) {
		fprintf(stderr, "usage: %s stock|none|stride|markov PAGES BUDGET_PAGES "
			"ROUNDS IDLE_MS DWELL_MS DELAY_US [CONTROLLER_CPU]\n", argv[0]);
		return EXIT_FAILURE;
	}
	stock = !strcmp(argv[1], "stock");
	pages = number(argv[2], 2, PMT_MAX_ARENA_PAGES);
	budget = number(argv[3], 1, pages);
	rounds = number(argv[4], 1, 1000000 / pages);
	count = pages * rounds;
	number(argv[5], 1, 3600000);
	number(argv[6], 0, 3600000);
	delay = number(argv[7], 0, 1000000);
	if (argc == 9)
		core = number(argv[8], 0, CPU_SETSIZE - 1);
	page_size = sysconf(_SC_PAGESIZE);
	if (page_size != 4096) {
		fputs("the arena requires 4 KiB Linux pages\n", stderr);
		return EXIT_FAILURE;
	}
	latency = calloc(count, sizeof(*latency));
	residency = malloc(pages);
	expected = calloc(pages, 1);
	requests = calloc(pages, 1);
	arena = mmap(NULL, pages * page_size, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (!latency || !residency || !expected || !requests || arena == MAP_FAILED ||
	    madvise(arena, pages * page_size, MADV_NOHUGEPAGE)) {
		perror("allocate arena");
		goto out;
	}

	if (!stock) {
		if (socketpair(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0, pair)) {
			perror("socketpair");
			goto out;
		}
		daemon = fork();
		if (daemon == 0)
			start_child(pair, argv, core);
		close(pair[1]);
		if (daemon < 0) {
			perror("fork");
			close(pair[0]);
			goto out;
		}
		connection = pair[0];
	}
	if (cgroup && join_workload_cgroup(cgroup)) {
		perror("join delegated workload cgroup");
		goto out;
	}

	began = monotonic_ns();
	for (size_t i = 0; i < count; i++) {
		size_t position = i % pages;
		uint32_t page = position % 2 ? pages - 1 - position / 2 : position / 2;
		volatile unsigned char *cell = arena + (size_t)page * page_size;
		unsigned char resident;
		struct pmt_observation obs;
		struct pmt_advice advice = {PMT_NO_PAGE, PMT_NO_PAGE};
		size_t occupied = 0;

		if (mincore((void *)cell, page_size, &resident)) {
			perror("mincore before access");
			goto out;
		}
		if (i >= pages) {
			samples++;
			nonresident += !(resident & 1);
		}
		if (requests[page]) {
			prefetch_resident += (resident & 1) != 0;
			prefetch_nonresident += (resident & 1) == 0;
			requests[page] = 0;
		}
		obs.timestamp_ns = monotonic_ns();
		obs.page = page;
		obs.wrote = i < pages || i % 17 == 0;
		if (*cell != expected[page]) {
			fputs("arena data changed across reclaim\n", stderr);
			goto out;
		}
		if (obs.wrote)
			*cell = ++expected[page];
		latency[i] = monotonic_ns() - obs.timestamp_ns;
		if (mincore(arena, pages * page_size, residency)) {
			perror("mincore arena");
			goto out;
		}
		for (size_t p = 0; p < pages; p++) {
			residency[p] &= 1;
			occupied += residency[p];
		}
		if (occupied > max_resident)
			max_resident = occupied;
		if (connection >= 0 && exchange(connection, &obs, residency, pages, &advice)) {
			disconnected = true;
			close(connection);
			connection = -1;
			kill(daemon, SIGTERM);
		}
		if (connection >= 0 && advice.evict != PMT_NO_PAGE &&
		    (occupied > budget || (occupied == budget && advice.prefetch != PMT_NO_PAGE))) {
			void *victim = arena + (size_t)advice.evict * page_size;

			evict++;
			if (madvise(victim, page_size, MADV_PAGEOUT) ||
			    mincore(victim, page_size, &resident))
				errors++;
			else if (!(resident & 1)) {
				evict_observed++;
				occupied--;
			}
		}
		if (connection >= 0 && advice.prefetch != PMT_NO_PAGE && occupied < budget &&
		    !requests[advice.prefetch]) {
			void *candidate = arena + (size_t)advice.prefetch * page_size;

			if (madvise(candidate, page_size, MADV_WILLNEED)) {
				errors++;
			} else {
				prefetch++;
				requests[advice.prefetch] = 1;
			}
		}
		if (delay) {
			struct timespec pause = {.tv_sec = delay / 1000000,
						 .tv_nsec = delay % 1000000 * 1000};

			nanosleep(&pause, NULL);
		}
	}
	elapsed = monotonic_ns() - began;
	if (connection >= 0) {
		close(connection);
		connection = -1;
	}
	if (daemon > 0) {
		disconnected |= reap_controller(daemon, &controller_usage) != 0;
		daemon = -1;
	}
	for (size_t page = 0; page < pages; page++)
		prefetch_unused += requests[page];
	/* Exclude allocation and first-touch demand times when there is a second pass. */
	size_t warmup = rounds > 1 ? pages : 0;
	size_t scored = count - warmup;
	qsort(latency + warmup, scored, sizeof(*latency), compare_latency);
	printf("{\"mode\":\"local_arena\",\"policy\":\"%s\",\"pages\":%zu,"
	       "\"budget_target_pages\":%zu,\"accesses\":%zu,\"repeat_accesses\":%zu,"
	       "\"nonresident_repeat_accesses\":%zu,\"max_resident_pages\":%zu,"
	       "\"p50_touch_ns\":%" PRIu64 ",\"p99_touch_ns\":%" PRIu64 ","
	       "\"eviction_attempts\":%zu,\"immediate_nonresident_after_pageout\":%zu,"
	       "\"prefetch_advice_accepted\":%zu,\"resident_on_prefetched_access\":%zu,"
	       "\"nonresident_on_prefetched_access\":%zu,\"unused_prefetch\":%zu,"
	       "\"advice_errors\":%zu,\"controller_disconnected\":%s,"
	       "\"cgroup_attached\":%s,\"controller_cpu_us\":%" PRIu64 ","
	       "\"controller_max_rss_kib\":%ld,\"elapsed_ns\":%" PRIu64 "}\n",
	       argv[1], pages, budget, count, samples, nonresident, max_resident,
	       latency[warmup + (scored * 50 + 99) / 100 - 1],
	       latency[warmup + (scored * 99 + 99) / 100 - 1], evict, evict_observed,
	       prefetch, prefetch_resident, prefetch_nonresident, prefetch_unused, errors,
	       disconnected ? "true" : "false", cgroup ? "true" : "false",
	       time_us(controller_usage.ru_utime) + time_us(controller_usage.ru_stime),
	       controller_usage.ru_maxrss, elapsed);
	status = disconnected ? EXIT_FAILURE : EXIT_SUCCESS;

out:
	if (connection >= 0)
		close(connection);
	if (daemon > 0)
		reap_controller(daemon, &controller_usage);
	if (arena != MAP_FAILED)
		munmap(arena, pages * page_size);
	free(requests);
	free(expected);
	free(residency);
	free(latency);
	return status;
}

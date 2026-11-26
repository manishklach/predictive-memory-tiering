KDIR ?= /lib/modules/$(shell uname -r)/build
PYTHON ?= python3
CFLAGS ?= -O2 -g

.PHONY: all module test test-reader test-local clean
all: pmtd pmt-local

pmtd: daemon/pmtd.c daemon/control.c include/pmt_uapi.h include/pmt_control.h
	$(CC) $(CPPFLAGS) -Iinclude $(CFLAGS) -std=c11 -Wall -Wextra -Werror $(filter %.c,$^) $(LDFLAGS) -o $@

libpmt-profile.so: daemon/control.c include/pmt_control.h
	$(CC) $(CPPFLAGS) -DPMT_PROFILE -Iinclude $(CFLAGS) -std=c11 -Wall -Wextra -Werror -shared -fPIC $< $(LDFLAGS) -o $@

pmtd-profile: daemon/pmtd.c daemon/control.c include/pmt_uapi.h include/pmt_control.h
	$(CC) $(CPPFLAGS) -DPMT_PROFILE -Iinclude $(CFLAGS) -std=c11 -Wall -Wextra -Werror $(filter %.c,$^) $(LDFLAGS) -o $@

pmt-local: harness/arena.c include/pmt_control.h
	$(CC) $(CPPFLAGS) -Iinclude $(CFLAGS) -std=c11 -Wall -Wextra -Werror $< $(LDFLAGS) -o $@

module:
	$(MAKE) -C "$(KDIR)" M="$(CURDIR)/kernel" W=1 modules

test: test-reader test-local checkpatch
	$(PYTHON) -m unittest discover -s traces -v

.PHONY: checkpatch
checkpatch:
	@if test -f "$(KDIR)/scripts/checkpatch.pl"; then \
		perl "$(KDIR)/scripts/checkpatch.pl" --no-tree --file kernel/pmt.c; \
	else echo "checkpatch skipped: unavailable in $(KDIR)"; fi

test-reader: pmtd
	$(PYTHON) harness/check_reader.py ./pmtd

test-local: pmtd pmt-local
	$(PYTHON) harness/check_controller.py ./pmtd ./pmt-local

clean:
	$(RM) pmtd libpmt-profile.so pmtd-profile pmt-local
	$(MAKE) -C "$(KDIR)" M="$(CURDIR)/kernel" clean

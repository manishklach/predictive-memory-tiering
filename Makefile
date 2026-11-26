KDIR ?= /lib/modules/$(shell uname -r)/build
PYTHON ?= python3
CFLAGS ?= -O2 -g

.PHONY: all module test test-reader clean
all: pmtd

pmtd: daemon/pmtd.c include/pmt_uapi.h
	$(CC) $(CPPFLAGS) -Iinclude $(CFLAGS) -std=c11 -Wall -Wextra -Werror $< $(LDFLAGS) -o $@

module:
	$(MAKE) -C "$(KDIR)" M="$(CURDIR)/kernel" W=1 modules

test: test-reader
	$(PYTHON) -m unittest discover -s traces -v

test-reader: pmtd
	$(PYTHON) harness/check_reader.py ./pmtd

clean:
	$(RM) pmtd
	$(MAKE) -C "$(KDIR)" M="$(CURDIR)/kernel" clean

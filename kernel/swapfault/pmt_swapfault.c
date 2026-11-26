// SPDX-License-Identifier: GPL-2.0
/* Bounded query-thread swap-fault attribution for the pinned Sydney kernel. */
#include <linux/sched.h>
#include <linux/delayacct.h>
#include <linux/fs.h>
#include <linux/kprobes.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>
#include <linux/tracepoint.h>

struct page_sample {
	unsigned long addr;
	u64 count, duration, delay, offcpu;
};

struct call_sample {
	struct page_sample *page;
	u64 start, delay;
};

static struct page_sample pages[64];
static unsigned int nr_pages;
static atomic_t owner = ATOMIC_INIT(0);

static struct tracepoint *switch_tp;
static struct page_sample *active_page;
static u64 slept_at;

static void find_switch(struct tracepoint *tp, void *unused)
{
	if (!strcmp(tp->name, "sched_switch"))
		switch_tp = tp;
}

static void switched(void *unused, bool preempt, struct task_struct *prev,
		     struct task_struct *next, unsigned int prev_state)
{
	struct page_sample *page = READ_ONCE(active_page);
	int pid = atomic_read(&owner);

	if (!page)
		return;
	if (task_pid_nr(prev) == pid && !preempt && prev_state)
		WRITE_ONCE(slept_at, ktime_get_ns());
	if (task_pid_nr(next) == pid && READ_ONCE(slept_at)) {
		page->offcpu += ktime_get_ns() - READ_ONCE(slept_at);
		WRITE_ONCE(slept_at, 0);
	}
}

static int fault_start(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct call_sample *s = (void *)ri->data;
	struct vm_fault *vmf;
	unsigned int i;

	if (task_pid_nr(current) != atomic_read(&owner))
		return 1;
	vmf = (void *)regs_get_kernel_argument(regs, 0);
	for (i = 0; i < nr_pages; i++) {
		if (pages[i].addr != (vmf->address & PAGE_MASK))
			continue;
		s->page = &pages[i];
		s->delay = current->delays->swapin_delay;
		s->start = ktime_get_ns();
		WRITE_ONCE(active_page, s->page);
		return 0;
	}
	return 1;
}

static int fault_end(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct call_sample *s = (void *)ri->data;

	WRITE_ONCE(active_page, NULL);
	s->page->duration += ktime_get_ns() - s->start;
	s->page->delay += current->delays->swapin_delay - s->delay;
	s->page->count++;
	return 0;
}

static struct kretprobe probe = {
	.kp.symbol_name = "do_swap_page",
	.entry_handler = fault_start,
	.handler = fault_end,
	.data_size = sizeof(struct call_sample),
	.maxactive = 256,
};

static int show(struct seq_file *m, void *unused)
{
	unsigned int i;

	if (task_pid_nr(current) != atomic_read(&owner))
		return -EPERM;
	seq_printf(m, "missed %d\n", probe.nmissed);
	for (i = 0; i < nr_pages; i++)
		seq_printf(m, "%u %llu %llu %llu %llu\n", i, pages[i].count,
			   pages[i].duration, pages[i].delay, pages[i].offcpu);
	return 0;
}

static int trace_open(struct inode *inode, struct file *file)
{
	int ret;

	if (!current->delays)
		return -ENODATA;
	if (atomic_cmpxchg(&owner, 0, task_pid_nr(current)))
		return -EBUSY;
	memset(pages, 0, sizeof(pages));
	nr_pages = 0;
	ret = single_open(file, show, NULL);
	if (!ret) {
		ret = register_kretprobe(&probe);
		if (!ret) {
			ret = tracepoint_probe_register(switch_tp, switched, NULL);
			if (!ret)
				return 0;
			unregister_kretprobe(&probe);
			probe.kp.addr = NULL;
		}
		single_release(inode, file);
	}
	atomic_set(&owner, 0);
	return ret;
}

static ssize_t mark(struct file *file, const char __user *buf, size_t size,
		    loff_t *offset)
{
	unsigned long addr;

	if (atomic_read(&owner) != task_pid_nr(current))
		return -EPERM;
	if (nr_pages >= ARRAY_SIZE(pages) || size != sizeof(addr))
		return -EINVAL;
	if (copy_from_user(&addr, buf, size))
		return -EFAULT;
	if (!PAGE_ALIGNED(addr) || !access_ok((void __user *)addr, PAGE_SIZE))
		return -EINVAL;
	pages[nr_pages++].addr = addr;
	return size;
}

static int trace_close(struct inode *inode, struct file *file)
{
	tracepoint_probe_unregister(switch_tp, switched, NULL);
	tracepoint_synchronize_unregister();
	unregister_kretprobe(&probe);
	probe.kp.addr = NULL;
	atomic_set(&owner, 0);
	return single_release(inode, file);
}

static const struct file_operations ops = {
	.owner = THIS_MODULE,
	.open = trace_open,
	.write = mark,
	.read = seq_read,
	.release = trace_close,
};
static struct miscdevice dev = {
	.minor = MISC_DYNAMIC_MINOR, .name = "pmt_swapfault",
	.fops = &ops, .mode = 0600,
};

static int __init start(void)
{
	for_each_kernel_tracepoint(find_switch, NULL);
	if (!switch_tp)
		return -ENOENT;
	return misc_register(&dev);
}
static void __exit stop(void)
{
	misc_deregister(&dev);
}
module_init(start);
module_exit(stop);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Bounded PMT swap-fault diagnostic");



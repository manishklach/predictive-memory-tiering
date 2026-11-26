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

struct page_sample {
	unsigned long addr;
	u64 count, duration, delay;
};

struct call_sample {
	struct page_sample *page;
	u64 start, delay;
};

static struct page_sample pages[64];
static unsigned int nr_pages;
static atomic_t owner = ATOMIC_INIT(0);

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
		return 0;
	}
	return 1;
}

static int fault_end(struct kretprobe_instance *ri, struct pt_regs *regs)
{
	struct call_sample *s = (void *)ri->data;

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
		seq_printf(m, "%u %llu %llu %llu\n", i, pages[i].count,
			   pages[i].duration, pages[i].delay);
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
		if (!ret)
			return 0;
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



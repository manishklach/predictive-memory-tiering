// SPDX-License-Identifier: GPL-2.0
#include <linux/debugfs.h>
#include <linux/fs.h>
#include <linux/kfifo.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pid.h>
#include <linux/pid_namespace.h>
#include <linux/poll.h>
#include <linux/sched.h>
#include <linux/seq_file.h>
#include <linux/uaccess.h>
#include <linux/workqueue.h>

#include "pmt_uapi.h"

static DEFINE_MUTEX(pmt_lock);
static DECLARE_WAIT_QUEUE_HEAD(pmt_wait);
static DEFINE_KFIFO(pmt_events, struct pmt_event, 256);
static struct dentry *pmt_debugfs;
static struct pid *pmt_target;
static bool pmt_enabled;
static bool pmt_reader;
static bool pmt_readable;
static unsigned int pmt_interval_ms = 500;
static u64 pmt_sequence;
static u64 pmt_emitted;
static u64 pmt_dropped;
static u64 pmt_self_disables;

static void pmt_sample(struct work_struct *work);
static DECLARE_DELAYED_WORK(pmt_work, pmt_sample);

static bool pmt_target_alive(void)
{
	struct task_struct *task;
	bool alive;

	rcu_read_lock();
	task = pid_task(pmt_target, PIDTYPE_PID);
	alive = task && !READ_ONCE(task->exit_state);
	rcu_read_unlock();
	return alive;
}

static void pmt_disable(const char *reason)
{
	if (!pmt_enabled)
		return;

	pmt_enabled = false;
	pmt_self_disables++;
	pr_info("pmt: disabled (%s)\n", reason);
}

static void pmt_sample(struct work_struct *work)
{
	struct pmt_event event;

	mutex_lock(&pmt_lock);
	if (!pmt_enabled)
		goto out;
	if (!pmt_target_alive()) {
		pmt_disable("target exited");
		goto out;
	}
	event.timestamp_ns = ktime_get_ns();
	event.page_id = pmt_sequence++;
	event.pid = pid_nr(pmt_target);
	event.abi = PMT_TRACE_ABI;
	event.access_type = PMT_ACCESS_SYNTHETIC;
	if (kfifo_put(&pmt_events, event)) {
		pmt_emitted++;
		WRITE_ONCE(pmt_readable, true);
		wake_up_interruptible(&pmt_wait);
	} else {
		pmt_dropped++;
	}
	schedule_delayed_work(&pmt_work, msecs_to_jiffies(pmt_interval_ms));
out:
	mutex_unlock(&pmt_lock);
}

static ssize_t target_pid_show(struct device *dev,
			       struct device_attribute *attr, char *buf)
{
	int nr;

	mutex_lock(&pmt_lock);
	nr = pid_nr(pmt_target);
	mutex_unlock(&pmt_lock);
	return sysfs_emit(buf, "%d\n", nr);
}

static ssize_t target_pid_store(struct device *dev,
				struct device_attribute *attr,
				const char *buf, size_t count)
{
	struct pid *target = NULL;
	struct pid *old;
	int nr, ret;

	ret = kstrtoint(buf, 10, &nr);
	if (ret || nr < 0)
		return -EINVAL;
	if (nr) {
		/* This interface and its records use host PID numbers. */
		if (task_active_pid_ns(current) != &init_pid_ns)
			return -EPERM;
		target = find_get_pid(nr);
		if (!target)
			return -ESRCH;
	}

	mutex_lock(&pmt_lock);
	if (pmt_enabled) {
		mutex_unlock(&pmt_lock);
		put_pid(target);
		return -EBUSY;
	}
	old = pmt_target;
	pmt_target = target;
	kfifo_reset(&pmt_events);
	WRITE_ONCE(pmt_readable, false);
	mutex_unlock(&pmt_lock);
	put_pid(old);
	return count;
}
static DEVICE_ATTR_RW(target_pid);

static ssize_t enabled_show(struct device *dev,
			    struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%u\n", READ_ONCE(pmt_enabled));
}

static ssize_t enabled_store(struct device *dev,
			     struct device_attribute *attr,
			     const char *buf, size_t count)
{
	bool enabled;
	int ret = kstrtobool(buf, &enabled);

	if (ret)
		return ret;

	mutex_lock(&pmt_lock);
	if (enabled && (!pmt_reader || !pmt_target_alive())) {
		mutex_unlock(&pmt_lock);
		return -ENODEV;
	}
	pmt_enabled = enabled;
	if (enabled)
		mod_delayed_work(system_wq, &pmt_work, 0);
	mutex_unlock(&pmt_lock);
	return count;
}
static DEVICE_ATTR_RW(enabled);

static ssize_t interval_ms_show(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	return sysfs_emit(buf, "%u\n", READ_ONCE(pmt_interval_ms));
}

static ssize_t interval_ms_store(struct device *dev,
				 struct device_attribute *attr,
				 const char *buf, size_t count)
{
	unsigned int interval;
	int ret = kstrtouint(buf, 10, &interval);

	if (ret || interval < 100 || interval > 10000)
		return -EINVAL;
	mutex_lock(&pmt_lock);
	pmt_interval_ms = interval;
	if (pmt_enabled)
		mod_delayed_work(system_wq, &pmt_work, msecs_to_jiffies(interval));
	mutex_unlock(&pmt_lock);
	return count;
}
static DEVICE_ATTR_RW(interval_ms);

static ssize_t inject_failure_store(struct device *dev,
				     struct device_attribute *attr,
				     const char *buf, size_t count)
{
	if (!sysfs_streq(buf, "1"))
		return -EINVAL;
	mutex_lock(&pmt_lock);
	pmt_disable("operator injected a test failure");
	mutex_unlock(&pmt_lock);
	return count;
}
static DEVICE_ATTR_WO(inject_failure);

static struct attribute *pmt_attrs[] = {
	&dev_attr_target_pid.attr,
	&dev_attr_enabled.attr,
	&dev_attr_interval_ms.attr,
	&dev_attr_inject_failure.attr,
	NULL,
};
ATTRIBUTE_GROUPS(pmt);

static int pmt_open(struct inode *inode, struct file *file)
{
	int ret = 0;

	if (!(file->f_mode & FMODE_READ) || (file->f_mode & FMODE_WRITE))
		return -EINVAL;

	mutex_lock(&pmt_lock);
	if (pmt_reader) {
		ret = -EBUSY;
	} else {
		kfifo_reset(&pmt_events);
		WRITE_ONCE(pmt_readable, false);
		pmt_reader = true;
	}
	mutex_unlock(&pmt_lock);
	return ret ? ret : nonseekable_open(inode, file);
}

static int pmt_release(struct inode *inode, struct file *file)
{
	mutex_lock(&pmt_lock);
	pmt_reader = false;
	pmt_disable("trace reader closed");
	mutex_unlock(&pmt_lock);
	return 0;
}

static ssize_t pmt_read(struct file *file, char __user *buf, size_t count,
			loff_t *pos)
{
	struct pmt_event event;
	int ret;

	if (!count)
		return 0;
	if (count < sizeof(event))
		return -EINVAL;

	for (;;) {
		if (mutex_lock_interruptible(&pmt_lock))
			return -ERESTARTSYS;
		if (kfifo_peek(&pmt_events, &event)) {
			ret = -EFAULT;
			if (!copy_to_user(buf, &event, sizeof(event))) {
				kfifo_skip(&pmt_events);
				WRITE_ONCE(pmt_readable, !kfifo_is_empty(&pmt_events));
				ret = sizeof(event);
			}
			mutex_unlock(&pmt_lock);
			return ret;
		}
		mutex_unlock(&pmt_lock);
		if (file->f_flags & O_NONBLOCK)
			return -EAGAIN;
		ret = wait_event_interruptible(pmt_wait, READ_ONCE(pmt_readable));
		if (ret)
			return ret;
	}
}

static __poll_t pmt_poll(struct file *file, poll_table *wait)
{
	poll_wait(file, &pmt_wait, wait);
	if (READ_ONCE(pmt_readable))
		return EPOLLIN | EPOLLRDNORM;
	return 0;
}

static const struct file_operations pmt_fops = {
	.owner = THIS_MODULE,
	.open = pmt_open,
	.release = pmt_release,
	.read = pmt_read,
	.poll = pmt_poll,
};

static struct miscdevice pmt_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "pmt_trace",
	.fops = &pmt_fops,
	.mode = 0600,
	.groups = pmt_groups,
};

static int pmt_stats_show(struct seq_file *file, void *data)
{
	mutex_lock(&pmt_lock);
	seq_printf(file, "synthetic_emitted %llu\ntrace_drops %llu\nself_disables %llu\n",
		   pmt_emitted, pmt_dropped, pmt_self_disables);
	mutex_unlock(&pmt_lock);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(pmt_stats);

static int __init pmt_init(void)
{
	int ret = misc_register(&pmt_device);

	if (ret)
		return ret;
	pmt_debugfs = debugfs_create_dir("pmt", NULL);
	debugfs_create_file("stats", 0400, pmt_debugfs, NULL, &pmt_stats_fops);
	return 0;
}

static void __exit pmt_exit(void)
{
	misc_deregister(&pmt_device);
	debugfs_remove_recursive(pmt_debugfs);
	mutex_lock(&pmt_lock);
	pmt_enabled = false;
	mutex_unlock(&pmt_lock);
	cancel_delayed_work_sync(&pmt_work);
	put_pid(pmt_target);
}

module_init(pmt_init);
module_exit(pmt_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Predictive memory tiering synthetic trace scaffold");

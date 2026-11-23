# Out-of-tree build of usbip-vudc only (fix for NULL deref in vep_dequeue, CVE-2026-64331)
# Sources: kernel v6.16.7 + upstream patch (vudc_dev.c, vudc.h)
obj-m := usbip-vudc.o
usbip-vudc-y := vudc_dev.o vudc_sysfs.o vudc_tx.o vudc_rx.o vudc_transfer.o vudc_main.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean

# Run with sudo; depmod gives updates/ precedence over kernel/
install-fix: all
	install -D -m 644 usbip-vudc.ko /lib/modules/$(shell uname -r)/updates/usbip-vudc.ko
	depmod -a
	@echo "Installed: $$(modinfo -F filename usbip-vudc)"
	@echo "Reboot (or rmmod/modprobe the usbip modules) to load the fixed version."

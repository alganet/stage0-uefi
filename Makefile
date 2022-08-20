# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

ESP_SIZE_MIB = 50
QEMU = qemu-system-x86_64
QEMU_KVM = -enable-kvm

ifneq ($(wildcard /usr/share/OVMF/OVMF_CODE.fd),)
    OVMF_IMG = /usr/share/OVMF/OVMF_CODE.fd
else
    OVMF_IMG = /usr/share/edk2-ovmf/OVMF_CODE.fd
endif

ESP_SIZE_SECTORS = $$(($(ESP_SIZE_MIB) * 2048))
DISK_SIZE_SECTORS = $$(($(ESP_SIZE_SECTORS) + 2048 + 33))

build_dir = build
rootfs_dir = $(build_dir)/rootfs
boot_dir = $(rootfs_dir)/EFI/BOOT

.PHONY : clean rootfs qemu

$(build_dir)/disk.img: $(build_dir)/esp.img
	dd if=/dev/zero of=$@ bs=512 count=$(DISK_SIZE_SECTORS)
	parted $@ -s -a minimal mklabel gpt
	parted $@ -s -a minimal mkpart EFI FAT32 2048s $(ESP_SIZE_SECTORS)s
	parted $@ -s -a minimal toggle 1 boot
	dd if=$< of=$@ bs=512 seek=2048 conv=notrunc
	@echo -e "\n"
	@echo "stage0-uefi disk image was created at" $@
	@echo -e "\nRun 'make qemu' to try it inside QEMU"

qemu: $(build_dir)/disk.img $(OVMF_IMG)
	$(QEMU) -cpu qemu64 -net none \
	$(QEMU_KVM) \
	-drive if=pflash,format=raw,unit=0,file=$(OVMF_IMG),readonly=on \
	-drive if=ide,format=raw,file=$<

$(build_dir)/esp.img: rootfs
	dd if=/dev/zero of=$@ bs=512 count=$(ESP_SIZE_SECTORS)
	mformat -i $@ -h 32 -t 32 -n 64 -c 1
	mcopy -s -i $@ $(rootfs_dir)/* ::

rootfs:
	rm -rf $(rootfs_dir)
	mkdir -p $(boot_dir)
	rsync -av . $(rootfs_dir) --exclude $(build_dir) --exclude ".*" --exclude "bootstrap-seeds/"

	mkdir -p $(rootfs_dir)/bootstrap-seeds/UEFI/
	rsync -av bootstrap-seeds/UEFI/ $(rootfs_dir)/bootstrap-seeds/UEFI/
ifndef MINIMAL
	mv $(rootfs_dir)/bootstrap-seeds/UEFI/amd64/kaem-optional-seed.efi $(boot_dir)/BOOTX64.efi
else
	rm $(rootfs_dir)/bootstrap-seeds/UEFI/amd64/kaem-optional-seed.efi
endif

clean:
	rm -rf $(build_dir)

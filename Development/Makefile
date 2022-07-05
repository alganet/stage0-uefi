# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

targets = hex0
ESP_SIZE = 50 # MiB
QEMU = qemu-system-x86_64
OVMF_IMG = /usr/share/edk2-ovmf/OVMF_CODE.fd

ESP_SIZE_SECTORS = $$(($(ESP_SIZE) * 2048))
DISK_SIZE_SECTORS=$$(($(ESP_SIZE_SECTORS) + 2048 + 33))

cc = clang
cflags = -ffreestanding -MMD -mno-red-zone -std=c11 -target x86_64-unknown-windows -Wall -Werror -pedantic
ld = lld-link
lflags = -subsystem:efi_application -nodefaultlib -dll
build_dir = build
rootfs_dir = $(build_dir)/rootfs

targets2 = $(addsuffix .efi, $(addprefix $(build_dir)/, $(targets)))

$(build_dir)/%.efi : $(build_dir)/%.o
	$(ld) $(lflags) -entry:efi_main $< -out:$@

$(build_dir)/%.o : %.c
	mkdir -p $(build_dir)
	$(cc) $(cflags) -c $< -o $@

.PHONY : clean rootfs qemu

qemu: $(build_dir)/disk.img $(OVMF_IMG)
	$(QEMU) -cpu qemu64 -enable-kvm -net none \
	-drive if=pflash,format=raw,unit=0,file=$(OVMF_IMG),readonly=on \
	-drive if=ide,format=raw,file=$<

all : $(targets2)

$(build_dir)/disk.img: $(build_dir)/esp.img
	dd if=/dev/zero of=$@ bs=512 count=$(DISK_SIZE_SECTORS)
	parted $@ -s -a minimal mklabel gpt
	parted $@ -s -a minimal mkpart EFI FAT32 2048s $(ESP_SIZE_SECTORS)s
	parted $@ -s -a minimal toggle 1 boot
	dd if=$< of=$@ bs=512 seek=2048 conv=notrunc

$(build_dir)/esp.img: rootfs
	dd if=/dev/zero of=$@ bs=512 count=$(ESP_SIZE_SECTORS)
	mformat -i $@ -h 32 -t 32 -n 64 -c 1
	mcopy -s -i $@ $(rootfs_dir)/* ::

rootfs: $(targets2)
	cp -R rootfs $(build_dir)
	mkdir -p $(rootfs_dir)/EFI/BOOT/
	cp $(build_dir)/hex0.efi $(rootfs_dir)/EFI/BOOT/BOOTX64.EFI

clean:
	rm -rf $(build_dir)

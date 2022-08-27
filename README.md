# stage0-uefi

This is a port of stage0-posix (https://github.com/oriansj/stage0-posix) to UEFI.

## Usage

`make build/disk.img` will create bootable images with stage0-uefi. You can use `dd` to write them on bootable media.

`make qemu` will create disk image and launch it in QEMU. To test this in QEMU, you need to provide some QEMU compatible UEFI implementation. In particular, you can use Tianocore's OVMF. `make qemu` is looking for it in a few paths where distros tend to install it. If not you can specify it via `make qemu OVMF_IMG=/path/to/OVMF_CODE.fd`

## Minimal images

It is possible to create images without `kaem-optional-seed.efi` that contain only `hex0-seed.efi`. Those are a bit less automated but if your UEFI implementation lets you pass command line arguments, you can recreate kaem using just hex0.

- `make MINIMAL=1 qemu`.
- Open UEFI settings (often F2 but it varies from system to system).
- Go to `Add Boot option` and pick appropriate boot volume that contains `hex0-seed.efi`.
- Pick `\bootstrap-seeds\UEFI\amd64\hex0-seed.efi`.
- Description can be anything, say `kaem-seed`.
- Optional Data has to be `hex0 amd64\kaem-minimal.hex0 EFI\BOOT\BOOTX64.efi`.
- Save boot entry and boot it. This will build kaem-minimal and exit.
- Now boot normally from bootable media.

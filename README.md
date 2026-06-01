# stage0-uefi

This is a port of stage0-posix (https://github.com/oriansj/stage0-posix) to UEFI. Its purpose is to start with a tiny binary seed that can be manually inspected and use it to build C toolchain and some extra tools. It targets amd64 and riscv64 UEFI.

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

## Stages

### kaem-optional (Optional "shell")

`kaem-optional` is a trivial shell that can read list of commands together with their command line arguments from a file and executes them. It also supports line comments but has no other features.

### hex0

`hex0` is fairly trivial to implement and for each pair of hexadecimals characters it outputs a byte. We have also added two types of line comments (# and ;) to create a well commented lines like

```
    # :loop_options [_start + 0x6F]
    4839D3          ; cmp_rbx,rdx                 # Check if we are done
    74 14           ; je !loop_options_done       # We are done
    4883EB 02       ; sub_rbx, !2                 # --options
```

In the first steps we use initial `hex0` binary seed to rebuild `kaem-optional` and `hex0` from their source.

`hex0` code is somewhat tedious to read and write as it is basically a well documented machine code. We have to manually calculate all jumps in the code.

### hex1

This is the last program that has to be written in `hex0` language. `hex1` is a simple extension of `hex0` and adds a single character labels and allows calculating 32-bit offsets from current position in the code to the label. `hex1` code might look like

```
:a #:loop_options
    4839D3          ; cmp_rbx,rdx                 # Check if we are done
    0F84 %b         ; je %loop_options_done       # We are done
    4883EB 02       ; sub_rbx, !2                 # --options
```

### hex2

`hex2` is our final hex language that adds support for labels of arbitrary length. It also allows accessing them via 8, 16, 32-bit relative addresses (!, @, %) and via 16-bit or 32-bit ($, &) absolute addresses though only the former addressing mode is used in `stage0-uefi`.

```
:loop_options
    4839D3                ; cmp_rbx,rdx                        # Check if we are done
    74 !loop_options_done ; je8 !loop_options_done             # We are done
    4883EB 02             ; sub_rbx, !2                        # --options
```

### catm

`catm` allows concatenating files via `catm.efi output_file input1 input2 ... inputN`. This allows us to distribute shared code in separate files. We will first use it to append the `PE` header to `.hex2` files. Before this step PE header had to be included in the source file itself.

### M0

The `M0` assembly language is the simplest assembly language you can create that enables the creation of more complicated programs. It includes only a single keyword: `DEFINE` and leverages the language properties of `hex2` along with extending the behavior to populate immediate values of various sizes and formats.

Thus `M0` code looks like

```
DEFINE cmp_rbx,rdx 4839D3
DEFINE je 0F84
DEFINE sub_rbx, 4881EB

:loop_options
    cmp_rbx,rdx                         # Check if we are done
    je %loop_options_done               # We are done
    sub_rbx, %2                         # --options
```

### cc_amd64

The `cc_amd64` implements a subset of the C language designed in `M0` assembly. It is a somewhat limited subset of C but complete enough to make it easy to write a more usable C compiler written in the C subset that `cc_amd64` supports. (riscv64 builds the analogous `cc_riscv64`.)

At this stage we start using `M2libc` (https://github.com/oriansj/M2libc/) as our C library. In fact, `M2libc` ships two versions of C library. At this stage we use a single-file (`bootstrap.c`) C library that contains just enough to build `M2-Planet`.

### M2-Planet

This is the only C program that we build with `cc_amd64`. M2-Planet (https://github.com/oriansj/M2-Planet) supports a larger subset of C than `cc_amd64` and we are somewhat closer to C89 (it does not implement all C89 features but on the other hand it does have some C99 features). `M2-Planet` also includes a very basic preprocessor, so we can use stuff like `#define`, `#ifdef`.

`M2-Planet` supports generating code for various architectures including `x86`, `amd64`, `armv7`, `aarch64`, `riscv32` and `riscv64`.

`M2-Planet` is also capable of using full `M2libc` C library that has more features and optimizations compared to bootstrap version of `M2libc`.

`M2libc` hides all UEFI specific bits inside it, so that applications written for POSIX (such as `M2-Planet`) can run without any source modifications.

### C versions of linker and assembler

We then build C version of `hex2` (also called `hex2`) and C version of `M0` called `M1`. These are more capable than their platform specific hex counterparts and are fully cross-platform. Thus we can now have the whole toolchain written in C.

### kaem

We now build `kaem` which is a more capable version of `kaem-optional` and adds support for variables, environmental variables, conditionals and aliases. It also has various built-ins such as `cd` and `echo`.

### M2-Planet (built against full M2libc)

We can now rebuild `M2-Planet` so that it itself can benefit from full `M2libc`.

### M2-Mesoplanet

`M2-Mesoplanet` is a preprocessor that is more capable than `M2-Planet` and supports `#include` statements. It can also launch compiler, assembler and linker with the correct arguments, so we don't need to invoke them
manually.

### blood-elf

`blood-elf` is a tool that can generate a bit of debug info for POSIX binaries. It is not immediately useful for UEFI but
it might be useful if one wants to build debuggable POSIX binaries on UEFI.

### mescc-tools-extra

Some extra tools such as `sha256sum`, `untar`, `ungz`, `unbz2`, `mkdir`, `rm` and a few others.

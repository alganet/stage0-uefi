# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Usage hex0 file.hex0 file
# Does not validate the arguments

# Calling convention:
# First four arguments are passed via registers rcx, rdx, r8, r9 (if they fit in 64-bits)
# but we need to leave stack space

.global _start
.text

_start:
        mov  [ImageHandle], rcx            # ImageHandle *image_handle
        mov  rax, [rdx+96]                 # system->boot
        mov  [SystemBoot], rax             # save system->boot

        # Open Loaded Image protocol
        sub  rsp, 72                       # allocate stack
                                           # arg1 = ImageHandle
        mov  rdx, [LOADED_IMAGE_PROTOCOL]  # EFI_LOADED_IMAGE_PROTOCOL_GUID (first 64 bits)
        mov  [rsp+48], rdx                 # save onto stack
        mov  rdx, [LOADED_IMAGE_PROTOCOL+8]# EFI_LOADED_IMAGE_PROTOCOL_GUID (last 64 bits)
        mov  [rsp+56], rdx                 # save onto stack
        lea  rdx, [rsp+48]                 # arg2 = &EFI_LOADED_IMAGE_PROTOCOL_GUID
        lea  r8, [rsp+64]                  # arg3 = &image
        mov  r9, rcx                       # arg4 = image_handle
        mov  qword ptr [rsp+32], 0         # arg5 = NULL
        mov  qword ptr [rsp+40], 1         # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL

        call [rax+280]                     # system->boot->open_protocol();

        # Command line args
        mov  rax, [rsp+64]                 # get image
        mov  rcx, rax                      # save image
        mov  rax, [rax+56]                 # options = image->load_options

loop_options1:                             # Skip application name
        add  rax, 2                        # ++options
        mov  bl, [rax]                     # *options
        cmp  bl, 0x20                      # if *options != ' '
        jne  loop_options1                 # then jump

        add  rax, 2                        # ++options
        push rax                           # in = options

loop_options2:                             # Skip argv[1]
        add  rax, 2                        # ++options
        mov  bl, [rax]                     # *options
        cmp  bl, 0x20                      # if *options != ' '
        jne  loop_options2                 # then jump

        mov  qword ptr [rax],              # *options = 0;
        add  rax, 2                        # ++options
        push rax                           # out = options

        # Get root device
        sub  rsp, 72                       # allocate stack
        mov  rcx, [rcx+24]                 # arg1 = root_device = image->device
        mov  rdx, [SIMPLE_FS_PROTOCOL]     # EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID (first 64 bits)
        mov  [rsp+48], rdx                 # save onto stack
        mov  rdx, [SIMPLE_FS_PROTOCOL+8]   # EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID (last 64 bits)
        mov  [rsp+56], rdx                 # save onto stack
        lea  rdx, [rsp+48]                 # arg2 = &EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID
        lea  r8, [rsp+64]                  # arg3 = &root_fs
        mov  r9, [ImageHandle]             # arg4 = image_handle
        mov  qword ptr [rsp+32], 0         # arg5 = NULL
        mov  qword ptr [rsp+40], 1         # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL

        mov  rax, [SystemBoot]             # system->boot
        call [rax+280]                     # system->boot->open_protocol();
        mov  r8, [rsp+64]                  # get root_fs
        add  rsp, 144                      # deallocate stack

        pop  r8                            # out
        pop  r8                            # in

Done:
        ret

.data

ImageHandle: .quad 0
SystemBoot: .quad 0

# Protocol GUIDs
LOADED_IMAGE_PROTOCOL:
.long  0x5b1b31a1
.short 0x9562
.short 0x11d2
.byte 0x8e, 0x3f, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

SIMPLE_FS_PROTOCOL:
.long  0x0964e5b22
.short 0x6459
.short 0x11d2
.byte 0x8e, 0x39, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

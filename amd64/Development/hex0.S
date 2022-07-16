# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
# SPDX-FileCopyrightText: 2017 Jeremiah Orians <jeremiah@pdp10.guru>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Usage: hex0 file.hex0 file
# Does not validate the arguments or check for success

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
        add  rsp, 72                       # deallocate stack
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

        mov  byte ptr [rax], 0             # *options = 0;
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
        lea  r8, [rsp+64]                  # arg3 = &rootfs
        mov  r9, [ImageHandle]             # arg4 = image_handle
        mov  qword ptr [rsp+32], 0         # arg5 = NULL
        mov  qword ptr [rsp+40], 1         # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL

        mov  rax, [SystemBoot]             # system->boot
        call [rax+280]                     # system->boot->open_protocol();

        # Get root fs
        mov  rcx, [rsp+64]                 # get rootfs
        add  rsp, 72                       # deallocate stacks

        sub  rsp, 24                       # allocate stack
        lea  rdx, [RootDir]                # arg2 = &rootdir
        call [rcx+8]                       # rootfs->open_volume(rootfs, &rootdir)

        # Open file for writing
        add  rsp, 24                       # deallocate stack

        mov  rcx, [RootDir]                # arg1 = rootdir
        pop  r8                            # arg3 = out
        sub  rsp, 40                       # allocate stack
        lea  rdx, [fout]                   # arg2 = &fout
        mov  r9, 0x8000000000000003        # arg4 = EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ
        mov  qword ptr [rsp+32], 0         # arg5 = 0
        call [rcx+8]                       # rootdir->open()

        # Open file for reading
        add  rsp, 40                       # deallocate stack

        mov  rcx, [RootDir]                # arg1 = rootdir
        pop  r8                            # arg3 = in
        sub  rsp, 40                       # allocate stack
        lea  rdx, [fin]                    # arg2 = &fin
        mov  r9, 1                         # arg4 = EFI_FILE_MODE_READ
        mov  qword ptr [rsp+32], 1         # arg5 = EFI_FILE_READ_ONLY
        call [rcx+8]                       # rootdir->open()
        add  rsp, 40                       # deallocate stack

        # Our flag for byte processing
        push -1
        pop r15                            # r15 = -1

        # temp storage for the sum
        xor r14d, r14d                     # r14 = 0

loop:
        # Read a byte
        call read_byte

        # process byte
        call hex

        # Deal with -1 values
        test rax, rax
        jl loop

        # deal with toggle
        test r15, r15
        jge print

        # process first byte of pair
        mov r14, rax
        xor r15d, r15d                     # r15 = 0
        jmp loop

# process second byte of pair
print:
        # update the sum and store in output
        shl r14, 4
        add rax, r14

        # flip the toggle
        push -1
        pop r15                            # r15 = -1

        call write_byte

        jmp loop

hex:
        # Purge Comment Lines (#)
        cmp rax, 35
        je purge_comment

        # Purge Comment Lines (;)
        cmp rax, 59
        je purge_comment

        # deal all ascii less than 0
        cmp rax, 48
        jl ascii_other

        # deal with 0-9
        cmp rax, 58
        jl ascii_num

        # deal with all ascii less than A
        cmp rax, 65
        jl ascii_other

        # deal with A-F
        cmp rax, 71
        jl ascii_high

        # deal with all ascii less than a
        cmp rax, 97
        jl ascii_other

        # deal with a-f
        cmp rax, 103
        jl ascii_low

        # The rest that remains needs to be ignored
        jmp ascii_other

purge_comment:
        # Read a byte
        call read_byte

        # Loop if not LF (works for CR/LF and LF/CR endings too)
        cmp rax, 10
        jne purge_comment

        # Otherwise return -1
        push -1
        pop rax                            # rax = -1
        ret

ascii_num:
        sub rax, 48
        ret

ascii_low:
        sub rax, 87
        ret

ascii_high:
        sub rax, 55
        ret

ascii_other:
        push -1
        pop rax                            # rax = -1
        ret

terminate:
        sub  rsp, 8                        # allocate stack
        mov  rcx, [fin]                    # arg1 = fin
        call [rcx+16]                      # fin->close()
        mov  rcx, [fout]                   # arg1 = fout
        call [rcx+16]                      # fout->close()

        add  rsp, 16                       # deallocate stack
        ret                                # return to UEFI

read_byte:
        mov  rcx, [fin]                    # arg1 = fin
        push 1                             # size = 1
        mov  rdx, rsp                      # arg2 = &size
        push rbx                           # allocate stack
        mov  r8, rsp                       # arg3 = &input
        sub  rsp, 24                       # allocate stack
        call [rcx+32]                      # fin->read()
        add  rsp, 24                       # deallocate stack
        pop  rax                           # save input to rax
        pop  rbx                           # save size to rbx

        # If the file ended (0 bytes read) terminate
        test rbx, rbx                      # if size == 0
        je   terminate                     # then we are done

        ret                                # return

# Writes byte stored in al
write_byte:
        mov  rcx, [fout]                   # arg1 = fout
        push 1                             # size = 1
        mov  rdx, rsp                      # arg2 = &size
        push rax                           # allocate stack
        mov  r8, rsp                       # arg3 = &output
        sub  rsp, 24                       # allocate stack
        call [rcx+40]                      # fout->write()
        add  rsp, 40                       # deallocate stack

        ret                                # return
.data

ImageHandle: .quad 0
SystemBoot: .quad 0
RootDir: .quad 0
fin: .quad 0
fout: .quad 0

# Protocol GUIDs
LOADED_IMAGE_PROTOCOL:
.long  0x5b1b31a1
.short 0x9562
.short 0x11d2
.byte  0x8e, 0x3f, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

SIMPLE_FS_PROTOCOL:
.long  0x0964e5b22
.short 0x6459
.short 0x11d2
.byte  0x8e, 0x39, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

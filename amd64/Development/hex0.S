# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
# SPDX-FileCopyrightText: 2017 Jeremiah Orians <jeremiah@pdp10.guru>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# Usage: hex0 file.hex0 file
# Does not validate the arguments or check for success

# Calling convention:
# First four arguments are passed via registers rcx, rdx, r8, r9 (if they fit in 64-bits)
# but we need to leave stack space
# rax, rcx, rdx, r8, r9, r10 and r11 are volatile and can be changed by called function
# Stack must be 16-byte aligned before UEFI calls.

.intel_syntax noprefix
.global _start
.text

# efi_main(void *image_handle, struct efi_system_table *system)
_start:
        # Save non-volatile registers
        push rbp
        mov rbp, rsp
        push rbx
        push rdi
        push rsi
        push r12
        push r13
        push r14
        push r15

        mov r15, rcx                       # save image_handle
        mov r14, [rdx+96]                  # system->boot

        # Open Loaded Image protocol
        mov r9, r15                        # arg4 = image_handle
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        mov rcx, r9                        # arg1 = image_handle
        push rax                           # allocate stack for image
        mov r8, rsp                        # arg3 = &image
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+280]                     # system->boot->open_protocol(image_handle, &guid, &image, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        add rsp, 48                        # deallocate stack
        pop rdi                            # get image

        # Get root file system
        mov r9, r15                        # arg4 = image_handle
        lea rdx, [rip+SIMPLE_FS_PROTOCOL]  # guid = &SIMPLE_FS_PROTOCOL
        mov rcx, [rdi+24]                  # arg1 = root_device = image->device
        mov r13, rcx                       # save root_device
        push rax                           # allocate stack for rootfs
        mov r8, rsp                        # arg3 = &rootfs
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+280]                     # system->boot->open_protocol(root_device, &guid, &rootfs, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        add rsp, 48                        # deallocate stack
        pop rcx                            # get rootfs

        # Get root directory
        push rax                           # allocate stack for rootdir
        mov rdx, rsp                       # arg2 = &rootdir
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootfs->open_volume(rootfs, &rootdir)
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop rsi                            # get rootdir

        # Push command line arguments onto stack
        mov rbx, [rdi+56]                  # options = image->load_options
        mov rdx, rbx                       # save beginning of load_options
        add rbx, [rdi+48]                  # go to the end of load_options
loop_options:
        cmp rbx, rdx                       # Check if we are done
        je loop_options_done               # We are done
        sub rbx, 2                         # --options
        mov al, [rbx]                      # *options
        cmp al, 0x20                       # if *options != ' '
        jne loop_options                   # then continue looping
        mov BYTE PTR [rbx], 0              # zero it
        add rbx, 2                         # ++options
        push rbx                           # push another argument onto stack
        jmp loop_options                   # next argument
loop_options_done:

        # Open file for reading
        pop r8                             # arg3 = in
        and rsp, -16                       # align stack to 16 bytes
        push rdx                           # allocate stack for fin
        mov rdx, rsp                       # arg2 = &fin
        push 1                             # arg5 = EFI_FILE_READ_ONLY
        push 1                             # prepare to set arg4 to EFI_FILE_MODE_READ
        pop r9                             # arg4 = EFI_FILE_MODE_READ
        mov rcx, rsi                       # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        add rsp, 40                        # deallocate stack
        pop r12                            # get fin

        # Open file for writing
        pop r8                             # arg3 = out
        push r13                           # save root_device
        push rdx                           # allocate stack for fout
        mov rdx, rsp                       # arg2 = &fout
        push 0                             # arg5 = 0
        push 7                             # to get 0x8000000000000003 we set the rightmost 3 bits
        pop r9                             # and then do right rotation by 1
        ror r9                             # arg4 = EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ
        mov rcx, rsi                       # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        add rsp, 40                        # deallocate stack
        pop r13                            # get fout

        # Save variables that are needed for cleanup
        push r14                           # save system->boot
        push r15                           # save image_handle
        push rsi                           # save rootdir
        mov r15, rsp                       # save stack location for cleanup

        # Our flag for byte processing
        push -1
        pop rbx                            # rbx = -1

        # temp storage for the sum
        xor edi, edi                       # rdi = 0

loop:
        # Read a byte
        call read_byte

        # process byte
        call hex

        # Deal with -1 values
        test eax, eax
        jl loop

        # deal with toggle
        test ebx, ebx                      # jump if ebx >= 0
        jge print

        # process first byte of pair
        mov edi, eax
        xor ebx, ebx                       # rbx = 0
        jmp loop

# process second byte of pair
print:
        # update the sum and store in output
        shl edi, 4
        add eax, edi

        # flip the toggle
        dec ebx                            # rbx = -1

        call write_byte

        jmp loop

hex:
        # Purge Comment Lines (#)
        cmp al, 35
        je purge_comment

        # Purge Comment Lines (;)
        cmp al, 59
        je purge_comment

        # deal all ascii less than 0
        cmp al, 48
        jl ascii_other

        # deal with 0-9
        cmp al, 58
        jl ascii_num

        # deal with all ascii less than A
        cmp al, 65
        jl ascii_other

        # deal with A-F
        cmp al, 71
        jl ascii_high

        # deal with all ascii less than a
        cmp al, 97
        jl ascii_other

        # deal with a-f
        cmp al, 103
        jl ascii_low

        # The rest that remains needs to be ignored
        jmp ascii_other

purge_comment:
        # Read a byte
        call read_byte

        # Loop if not LF (works for CR/LF and LF/CR endings too)
        cmp al, 10
        jne purge_comment

        # Otherwise return -1

ascii_other:
        push -1
        pop rax                            # return = -1
        ret

ascii_num:
        sub al, 48
        ret

ascii_low:
        sub al, 32                         # convert to uppercase

ascii_high:
        sub al, 55
        ret

# Writes byte stored in al
write_byte:
        push rsp                           # align stack to 16 bytes
        push [rsp]                         # align stack to 16 bytes
        and rsp, -16                       # align stack to 16 bytes
        push rax                           # make sure next call is aligned to 16 bytes
        mov rcx, r13                       # arg1 = fout
        push 1                             # size = 1
        mov rdx, rsp                       # arg2 = &size
        push rax                           # allocate stack
        mov r8, rsp                        # arg3 = &output
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+40]                      # fout->write()
        mov rsp, [rsp+56]                  # deallocate stack

        ret                                # return

read_byte:
        push rsp                           # align stack to 16 bytes
        push [rsp]                         # align stack to 16 bytes
        and rsp, -16                       # align stack to 16 bytes
        push rax                           # make sure next call is aligned to 16 bytes
        mov rcx, r12                       # arg1 = fin
        push 1                             # size = 1
        mov rdx, rsp                       # arg2 = &size
        xor esi, esi                       # zero rsi
        push rsi                           # allocate stack
        mov r8, rsp                        # arg3 = &input
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+32]                      # fin->read()
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop rax                            # save input to rax
        pop rsi                            # save size to rsi
        mov rsp, [rsp+16]                  # deallocate stack

        # If the file ended (0 bytes read) terminate
        test esi, esi                      # if size == 0
        je terminate                       # then we are done

        ret                                # return

terminate:
        mov rsp, r15                       # restore stack location for cleanup
        pop rsi                            # restore rootdir
        pop r15                            # restore image_handle
        pop r14                            # restore system->boot
        pop rbx                            # restore root_device

        push rbx                           # allocate stack
        mov rcx, r12                       # arg1 = fin
        call [rcx+16]                      # fin->close()
        mov rcx, r13                       # arg1 = fout
        call [rcx+16]                      # fout->close()
        mov rcx, rsi                       # arg1 = rootdir
        call [rcx+16]                      # rootdir->close()

        mov r8, r15                        # arg3 = image_handle
        lea rdx, [rip+SIMPLE_FS_PROTOCOL]  # guid = &SIMPLE_FS_PROTOCOL
        mov rcx, rbx                       # arg1 = root_device
        xor r9, r9                         # arg4 = NULL
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+288]                     # system->boot->close_protocol(root_device, &guid, image_handle, 0)

        mov r8, r15                        # arg3 = image_handle
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        mov rcx, r8                        # arg1 = image_handle
        xor r9, r9                         # arg4 = NULL
        call [r14+288]                     # system->boot->close_protocol(image_handle, &guid, image_handle, 0)

        # Restore non-volatile registers
        pop r15
        pop r14
        pop r13
        pop r12
        pop rsi
        pop rdi
        pop rbx

abort: # used for debugging only
        mov rsp, rbp                       # restore stack
        pop rbp
        ret                                # return to UEFI


# Protocol GUIDs
LOADED_IMAGE_PROTOCOL:
.long  0x5b1b31a1
.short 0x9562
.short 0x11d2
.byte  0x8e, 0x3f, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

SIMPLE_FS_PROTOCOL:
.long  0x964e5b22
.short 0x6459
.short 0x11d2
.byte  0x8e, 0x39, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

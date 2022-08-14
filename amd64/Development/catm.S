# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
# SPDX-FileCopyrightText: 2019 Jeremiah Orians
#
# SPDX-License-Identifier: GPL-3.0-or-later


.global _start
.text

# efi_main(void *image_handle, struct efi_system_table *system)
_start:
        mov rbp, rsp                       # save stack pointer
        mov r15, rcx                       # save image_handle
        mov r14, [rdx+96]                  # system->boot

        # Open Loaded Image protocol
        push rax                           # allocate stack for image
        mov r8, rsp                        # arg3 = &image
        mov rdx, [rip+LOADED_IMAGE_PROTOCOL+8] # EFI_LOADED_IMAGE_PROTOCOL_GUID (last 64 bits)
        push rdx                           # push last 64 bits onto stack
        mov rdx, [rip+LOADED_IMAGE_PROTOCOL] # EFI_LOADED_IMAGE_PROTOCOL_GUID (first 64 bits)
        push rdx                           # push first 64 bits onto stack
        mov rdx, rsp                       # arg2 = &guid
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        mov r9, r15                        # arg4 = image_handle
        mov rcx, r15                       # arg1 = image_handle
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+280]                     # system->boot->open_protocol(image_handle, &guid, &image, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        mov rax, [rsp+64]                  # get image
        mov rdi, rax                       # save image

        # Get root file system
        push rax                           # allocate stack for rootfs
        mov r8, rsp                        # arg3 = &rootfs
        mov rdx, [rip+SIMPLE_FS_PROTOCOL+8] # EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID (last 64 bits)
        push rdx                           # push last 64 bits onto stack
        mov rdx, [rip+SIMPLE_FS_PROTOCOL]  # EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID (first 64 bits)
        push rdx                           # push first 64 bits onto stack
        mov rdx, rsp                       # arg2 = &guid
        push 1                             # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                             # arg5 = NULL
        mov r9, r15                        # arg4 = image_handle
        mov rcx, [rdi+24]                  # arg1 = root_device = image->device
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [r14+280]                     # system->boot->open_protocol(root_device, &guid, &rootfs, image_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        mov rcx, [rsp+64]                  # get rootfs

        push rdx                           # allocate stack for rootdir
        mov rdx, rsp                       # arg2 = &rootdir
        push rax                           # allocate shadow stack space for UEFI function
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootfs->open_volume(rootfs, &rootdir)
        pop rax                            # deallocate stack
        pop rax                            # deallocate stack
        pop rax                            # get rootdir
        mov [rip+rootdir], rax             # save rootdir

        # Push command line arguments onto stack
        mov rbx, [rdi+56]                  # options = image->load_options
        mov rdx, rbx                       # save beginning of load_options
        add rbx, [rdi+48]                  # go to the end of load_options

        push 0                             # Save end of arguments (NULL) onto stack
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
        pop r8                             # get output file

        # Open file for writing
        push rdx                           # allocate stack for fout
        mov rdx, rsp                       # arg2 = &fout
        push 0                             # arg5 = 0
        push 7                             # to get 0x8000000000000003 we set the rightmost 3 bits
        pop r9                             # and then do right rotation by 1
        ror r9                             # arg4 = EFI_FILE_MODE_CREATE| EFI_FILE_MODE_WRITE | EFI_FILE_MODE_READ
        mov rcx, [rip+rootdir]             # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        add rsp, 40                        # deallocate stack
        pop r15                            # get fout

        mov rdx, 0x100000                  # Allocate 1MiB
        call allocate_pool                 # Get memory pool
        mov rbx, rax                       # Save it

core:
        pop r8                             # Get the actual input name
        cmp r8, 0                          # Check for null string
        je done                            # We are done if null string

        call open_file                     # Open file as read only
        test eax, eax                      # check if successfully opened
        jne core                           # Else go to another file
        mov r13, rsi                       # Protect fin

keep:
        mov rdx, 0x100000                  # set the size of chars we want
        call read                          # read them
        push rax                           # Protect the number of bytes read

        mov rdx, rax                       # number of bytes to write
        call write                         # write them

        pop rax                            # Get bytes read
        cmp rax, 0x100000                  # Check if buffer was fully used
        je keep                            # Keep looping if it was full

        mov rcx, r13                       # fin
        call close_file                    # close file
        jmp core                           # Move to next file

done:
        mov rcx, r15                       # Get output file
        call close_file                    # close it

        mov rcx, rbx                       # Get buffer
        call free_pool                     # release it
        xor eax, eax                       # Exit code 0

        mov rsp, rbp                       # restore stack pointer
        ret                                # return to UEFI

# r8: input file name
# returns input file handle in rsi, status in rax
open_file:
        # Open file for reading
        push rdx                           # allocate stack for fin
        mov rdx, rsp                       # arg2 = &fin
        push 1                             # arg5 = EFI_FILE_READ_ONLY
        push 1                             # prepare to set arg4 to EFI_FILE_MODE_READ
        pop r9                             # arg4 = EFI_FILE_MODE_READ
        mov rcx, [rip+rootdir]             # arg1 = rootdir
        sub rsp, 32                        # allocate shadow stack space for UEFI function
        call [rcx+8]                       # rootdir->open()
        add rsp, 40                        # deallocate stack
        pop rsi                            # get fin
        ret

# rcx: file handle
close_file:
        push rax                           # allocate shadow stack space for UEFI function
        call [rcx+16]                      # file_handle->close(file_handle)
        pop rax                            # deallocate stack
        ret

# rdx: number of bytes to allocate
# r14: system->boot
# returns pointer in rax
allocate_pool:
        push rdx                           # allocate stack for pool pointer
        mov r8, rsp                        # arg3 = &pool
        push 2
        pop rcx                            # arg1 = EFI_LOADER_DATA
        sub rsp, 24                        # allocate shadow stack space for UEFI
        call [r14+64]                      # system->boot->allocate_pool(EFI_LOADER_DATA, 2048, &pool)
        add rsp, 24                        # deallocate stack
        pop rax                            # get pool
        ret

# rcx: memory pool
# r14: system->boot
free_pool:
        push rax                           # allocate shadow stack space for UEFI function
        call [r14+72]                      # system->boot->free_pool(pool)
        pop rax                            # deallocate stack
        ret

# rdx: number of bytes to read
# rbx: buffer
# r13: input file handle
# returns number of bytes read in rax
read:
        mov rcx, r13                       # arg1 = fin
        push rdx                           # set size
        mov rdx, rsp                       # arg2 = &size
        mov r8, rbx                        # arg3 = buffer
        sub rsp, 24                        # allocate shadow stack space for UEFI
        call [rcx+32]                      # fin->read()
        add rsp, 24                        # deallocate stack
        pop rax                            # save size to rax
        ret

# rdx: number of bytes to write
# rbx: buffer
# r14: output file handle
# returns number of bytes written in rax
write:
        mov rcx, r15                       # arg1 = fout
        push rdx                           # set size
        mov rdx, rsp                       # arg2 = &size
        mov r8, rbx                        # arg3 = buffer
        sub rsp, 24                        # allocate shadow stack space for UEFI
        call [rcx+40]                      # fin->write()
        add rsp, 24                        # deallocate stack
        pop rax                            # save size to rax
        ret

.data

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

rootdir:
.long 0, 0

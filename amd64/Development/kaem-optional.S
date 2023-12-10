# SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
# SPDX-FileCopyrightText: 2017 Jeremiah Orians <jeremiah@pdp10.guru>
#
# SPDX-License-Identifier: GPL-3.0-or-later

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

        mov [rip+image_handle], rcx       # save image_handle
        mov rbx, rcx                      # save image_handle
        mov rax, [rdx+64]                 # system->out
        mov [rip+system_out], rax         # save system->out
        mov r14, [rdx+96]                 # system->boot

        xor ecx, ecx                      # timeout = 0
        xor edx, edx                      # watchdog_code = 0
        xor r8, r8                        # data_size = 0
        xor r9, r9                        # watchdog_data = 0
        sub  rsp, 32                      # allocate shadow stack space for UEFI function
        call [r14+240]                    # system->boot->set_watchdog_timer

        # Open Loaded Image protocol
        mov r9, rbx                       # arg4 = image_handle
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        mov rcx, rbx                      # arg1 = image_handle
        call open_protocol                # open protocol
        mov rdi, rax                      # save image
        mov [rip+image], rax              # save image

        # Get root file system
        mov r9, rbx                       # arg4 = image_handle
        lea rdx, [rip+SIMPLE_FS_PROTOCOL] # guid = &SIMPLE_FS_PROTOCOL
        mov rcx, [rdi+24]                 # arg1 = root_device = image->device
        mov [rip+root_device], rcx        # save root_device
        call open_protocol                # open protocol
        mov rcx, rax                      # get rootfs

        # Get root directory
        lea rdx, [rip+rootdir]            # arg2 = &rootdir
        push rax                          # align stack to 16 bytes
        push rax                          # allocate shadow stack space for UEFI function
        push rax                          # allocate shadow stack space for UEFI function
        call [rcx+8]                      # rootfs->open_volume(rootfs, &rootdir)
        pop rax                           # deallocate stack
        pop rax                           # deallocate stack

        # Push command line arguments onto stack
        mov rbx, [rdi+56]                 # options = image->load_options
        mov rdx, rbx                      # save beginning of load_options
        add rbx, [rdi+48]                 # go to the end of load_options
        push 0                            # Save end of arguments (NULL) onto stack
loop_options:
        cmp rbx, rdx                      # Check if we are done
        je loop_options_done              # We are done
        sub rbx, 2                        # --options
        mov al, [rbx]                     # *options
        cmp al, 0x20                      # if *options != ' '
        jne loop_options                  # then continue looping
        mov BYTE PTR [rbx], 0             # zero it
        add rbx, 2                        # ++options
        push rbx                          # push another argument onto stack
        jmp loop_options                  # next argument
loop_options_done:

        pop r8                            # get input file
        test r8, r8                       # Check if argument is specified
        jne arg_done                      # then use it
        # Else use default_file
        lea r8, [rip+default_file]        # Use "kaem.amd64"
arg_done:

        # Open file for reading
        and rsp, -16                      # align stack to 16 bytes
        push rdx                          # allocate stack for fin
        mov rdx, rsp                      # arg2 = &fin
        push 1                            # arg5 = EFI_FILE_READ_ONLY
        push 1                            # prepare to set arg4 to EFI_FILE_MODE_READ
        pop r9                            # arg4 = EFI_FILE_MODE_READ
                                          # arg3 = script
        mov rcx, [rip+rootdir]            # arg1 = rootdir
        sub rsp, 32                       # allocate shadow stack space for UEFI function
        call [rcx+8]                      # rootdir->open()
        test eax, eax                     # if status != EFI_SUCCESS
        jne terminate_2                   # then exit without closing file
        mov r12, [rsp+40]                 # get fin

        # Allocate pool for command
        xor edx, edx                      # zero rdx
        mov dh, 0x10                      # arg2 = 4096 = 0x1000
        call allocate_pool                # allocate memory
        mov rbx, rax                      # get command

next_command:
        xor esi, esi                      # i = 0
        xor r15, r15                      # command_length = 0

read_command:
        call read_byte                    # read another byte c

        cmp al, 0xa                       # if c == '\n'
        je read_command_done              # then we are done with this command

        cmp al, 0x20                      # if c == ' '
        jne read_command_comments
        test r15, r15                     # and command_length == 0
        jne read_command_comments
        mov r15, rsi                      # command_length = i

read_command_comments:
        cmp al, 0x23                      # if c = '#' then process comment
        jne read_command_store_char       # else store char

read_command_skip_comment:
        call read_byte                    # get another char
        cmp al, 0xa                       # if c != '\n'
        jne read_command_skip_comment     # continue reading until newline

        jmp next_command                  # deal with another line

read_command_store_char:
        add rbx, rsi                      # rbx = &command[i]
        mov [rbx], ax                     # command[i] = c
        sub rbx, rsi                      # rbx = &command[0]
        add rsi, 2                        # location of the next char
        jmp read_command                  # continue looping

read_command_done:
        test r15, r15                     # if command_length == 0
        je next_command                   # deal with another line

        add rbx, rsi                      # rbx = &command[i]
        mov WORD PTR [rbx], 0             # command[i] = 0
        sub rbx, rsi                      # rbx = &command[0]

        add rsi, 2                        # add 2 to get string length with NULL terminator

        lea rdx, [rip+prefix]             # get prefix " +> "
        call File_Print                   # print it
        mov rdx, rbx                      # get command
        call File_Print                   # print it
        lea rdx, [rip+suffix]             # get suffix "\n\r"
        call File_Print                   # print it

        # Remove command line options
        add r15, rbx                      # go to the space separating command and its options
        mov WORD PTR [r15], 0             # zero it to hide command line options

        # Align stack to 16 bytes
        push rsp
        push [rsp]
        and rsp, -16

        # Open executable file for reading
        push rdx                          # allocate stack for fcmd
        mov rdx, rsp                      # arg2 = &fcmd
        push 1                            # arg5 = EFI_FILE_READ_ONLY
        push 1                            # prepare to set arg4 to EFI_FILE_MODE_READ
        pop r9                            # arg4 = EFI_FILE_MODE_READ
        mov r8, rbx                       # arg3 = command
        mov rcx, [rip+rootdir]            # arg1 = rootdir
        sub rsp, 32                       # allocate shadow stack space for UEFI function
        call [rcx+8]                      # rootdir->open()
        test eax, eax                     # if status != EFI_SUCCESS
        jne print_error                   # then exit
        add rsp, 40                       # deallocate stack
        pop rdi                           # get fcmd

        # Restore command line arguments
        mov WORD PTR [r15], 0x20          # restore command line options by readding ' '

        # Allocate pool for file_info
        xor edx, edx                      # zero rdx
        mov dh, 0x10                      # arg2 = 4096 = 0x1000
        call allocate_pool                # allocate memory
        mov r9, rax                       # get file_info (arg4 for get_info)

        # Get file info
        push rax                          # save file_info
        push rax                          # allocate stack for file_size
        mov r8, rsp                       # arg3 = &file_size
        mov QWORD PTR [r8], 0x1000        # file_size = 0x1000
        lea rdx, [rip+FILE_INFO_PROTOCOL] # arg2 = &EFI_FILE_INFO_PROTOCOL
        mov rcx, rdi                      # arg1 = fcmd
        sub rsp, 32                       # allocate shadow stack space for UEFI function
        call [rcx+64]                     # fcmd->get_info(fcmd, &guid, &file_size, file_info)
        add rsp, 40                       # deallocate stack
        pop rcx                           # restore file_info
        mov rdx, [rcx+8]                  # get file_size

        # Free file_info pool
        push rdx                          # save file_size onto stack
        push rax                          # allocate shadow stack space for UEFI function
        call [r14+72]                     # system->boot->free_pool(file_info)
        pop rax                           # deallocate stack
        pop rdx                           # restore file_size from stack (arg2 for allocate_pool)

        # Allocate pool for executable
        push rdx                          # save file_size onto stack
        call allocate_pool                # allocate memory
        mov r15, rax                      # get executable
        pop rdx                           # restore file_size

        # Load executable into memory
        push rdx                          # save file_size onto stack
        mov r8, r15                       # arg3 = executable
        mov rdx, rsp                      # arg2 = &file_size
        mov rcx, rdi                      # arg1 = fcmd
        push rax                          # allocate shadow stack space for UEFI function
        push rax                          # allocate shadow stack space for UEFI function
        push rax                          # allocate shadow stack space for UEFI function
        call [rcx+32]                     # fcmd->read(fcmd, &file_size, executable)
        pop rax                           # deallocate stack
        pop rax                           # deallocate stack
        pop rax                           # deallocate stack

        # Close fcmd
        push rax                          # allocate stack
        mov rcx, rdi                      # arg1 = fcmd
        call [rcx+16]                     # fcmd->close(fcmd)
        pop rax                           # restore stack

        pop rdi                           # restore file_size

        # Allocate memory for device_path struct
        push 28                           # 4 + sizeof(struct efi_device_path_protocol)
        pop rdx                           # arg2 = 28
        call allocate_pool                # allocate memory
        mov r8, rax                       # get device_path

        # Initialize struct
        mov BYTE PTR [rax], 1             # device_path->type = HARDWARE_DEVICE_PATH
        inc rax                           # next member
        mov BYTE PTR [rax], 3             # device_path->subtype = MEMORY_MAPPED
        inc rax                           # next member
        mov WORD PTR [rax], 24            # device_path->length = 24
        add rax, 2                        # next member
        mov DWORD PTR [rax], 1            # device_path->memory_type = EFI_LOADER_CODE
        add rax, 4                        # next member
        mov QWORD PTR [rax], r15          # device_path->start_address = executable
        add rax, 8                        # next member
        add r15, rdi                      # end_address = executable + file_size
        mov QWORD PTR [rax], r15          # device_path->end_address = end_address
        sub r15, rdi                      # restore r15 = executable
        add rax, 8                        # next member
        mov BYTE PTR [rax], 0x7f          # device_path[1].type = END_HARDWARE_DEVICE_PATH
        inc rax                           # next member
        mov BYTE PTR [rax], 0xff          # device_path[1].subtype = END_ENTIRE_DEVICE_PATH
        inc rax                           # next member
        mov WORD PTR [rax], 4             # device_path[1].length = 4

        # Load image
        push r8                           # save device_path
        push rax                          # allocate stack for child_ih
        push rsp                          # arg6 = &child_ih
        push rdi                          # arg5 = file size
        mov r9, r15                       # arg4 = executable
                                          # arg3 = device_path
        mov rdx, [rip+image_handle]       # arg2 = image_handle
        xor ecx, ecx                      # arg1 = 0
        sub rsp, 32                       # allocate shadow stack space for UEFI function
        call [r14+200]                    # system->boot->load_image()
        add rsp, 48                       # deallocate_stack
        pop rdi                           # save child_ih

        # Free device_path pool
        pop rcx                           # arg1 = device_path
        push rax                          # make sure next call is aligned to 16 bytes
        push rax                          # allocate shadow stack space for UEFI function
        call [r14+72]                     # system->boot->free_pool(device_path)
        pop rax                           # deallocate stack

        # Free executable pool
        mov rcx, r15                      # arg1 = executable
        push rax                          # allocate shadow stack space for UEFI function
        call [r14+72]                     # system->boot->free_pool(executable)
        pop rax                           # deallocate stack

        # Open Child Image
        mov r9, rdi                       # arg4 = child_ih
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        push rdx                          # save &LOADED_IMAGE_PROTOCOL
        mov rcx, r9                       # arg1 = child_ih
        call open_protocol                # open protocol

        mov [rax+56], rbx                 # child_image->load_options = command
        mov [rax+48], rsi                 # set child_image->load_options_size
        mov rcx, [rip+image]              # get image
        mov rcx, [rcx+24]                 # image->device
        mov [rax+24], rcx                 # child_image->device = image->device

        mov r8, rdi                       # arg3 = image_handle
        pop rdx                           # arg2 = &LOADED_IMAGE_PROTOCOL
        mov rcx, r8                       # arg1 = image_handle
        call close_protocol               # close protocol

        # Run command
        xor r8, r8                        # arg3 = 0 (ExitData)
        xor edx, edx                      # arg2 = 0 (ExitData size)
        mov rcx, rdi                      # arg1 = child_ih
        push rax                          # allocate shadow stack space for UEFI function
        push rax                          # allocate shadow stack space for UEFI function
        push rax                          # allocate shadow stack space for UEFI function
        call [r14+208]                    # system->boot->start_image()

        mov rsp, [rsp+8]                  # deallocate stack (alignment before open executable)

        test eax, eax                     # check if return code is 0
        jne print_error                   # print error and exit
        jmp next_command                  # process another line from kaem script

print_error:
        push rax                          # save exit code
        lea rdx, [rip+subprocess_error]   # get error message
        call File_Print                   # print it
        pop rax                           # restore exit code

# Close script file and exit
terminate:
        # Align stack to 16 bytes
        push rsp
        push [rsp]
        and rsp, -16

        # Free pool
        mov rcx, rbx                      # arg1 = command
        push rax                          # save exit code
        push rax                          # allocate shadow stack space for UEFI function
        call [r14+72]                     # system->boot->free_pool(commmand)

        mov rcx, r12                      # arg1 = fin
        call [rcx+16]                     # fin->close(fin)

        mov rcx, [rip+rootdir]            # arg1 = rootdir
        call [rcx+16]                     # rootdir->close(rootdir)

        pop rax                           # deallocate stack
        pop rax                           # restore exit code

# Exit without closing script file
terminate_2:
        mov r8, [rip+image_handle]        # arg3 = image_handle
        push r8                           # save image_handle
        lea rdx, [rip+SIMPLE_FS_PROTOCOL] # guid = &SIMPLE_FS_PROTOCOL
        mov rcx, [rip+root_device]        # arg1 = root_device
        call close_protocol               # close protocol

        pop r8                            # arg3 = image_handle
        lea rdx, [rip+LOADED_IMAGE_PROTOCOL] # guid = &LOADED_IMAGE_PROTOCOL
        mov rcx, r8                       # arg1 = image_handle
        call close_protocol               # close protocol

        # Restore non-volatile registers
        pop r15
        pop r14
        pop r13
        pop r12
        pop rsi
        pop rdi
        pop rbx

abort:
        mov rsp, rbp                      # restore stack
        pop rbp
        ret                               # return to UEFI


# File_Print function
# Receives WCHAR* in RDX
File_Print:
        push rsp                          # align stack to 16 bytes
        push [rsp]                        # align stack to 16 bytes
        and rsp, -16                      # align stack to 16 bytes
        mov rcx, [rip+system_out]         # get system_out
        push rax                          # allocate shadow stack space for UEFI function
        push rax                          # allocate shadow stack space for UEFI function
        call [rcx+8]                      # system->out->output_string(system->out, WCHAR*)
        mov rsp, [rsp+24]                 # deallocate stack
        ret

# read_byte function
# reads a single character
read_byte:
        mov rcx, r12                      # arg1 = fin
        push 1                            # size = 1
        mov rdx, rsp                      # arg2 = &size
        push 0                            # allocate stack
        mov r8, rsp                       # arg3 = &c
        push rsp                          # align stack to 16 bytes
        push [rsp]                        # align stack to 16 bytes
        and rsp, -16                      # align stack to 16 bytes
        sub rsp, 32                       # allocate shadow stack space for UEFI and align stack
        call [rcx+32]                     # fin->read()
        mov rsp, [rsp+40]                 # deallocate stack
        pop rax                           # save c to rax
        pop rcx                           # save size to rcx

        # If the file ended (0 bytes read) terminate
        test ecx, ecx                     # if size == 0
        je terminate                      # then we are done

        ret

# rcx: handle
# rdx: &guid
# r9: agent_handle
# returns interface
open_protocol:
        push rax                          # allocate stack for interface
        mov r8, rsp                       # arg3 = &interface
        push rsp                          # align stack to 16 bytes
        push [rsp]                        # align stack to 16 bytes
        and rsp, -16                      # align stack to 16 bytes
        push 1                            # arg6 = EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL
        push 0                            # arg5 = NULL
        sub rsp, 32                       # allocate shadow stack space for UEFI function
        call [r14+280]                    # system->boot->open_protocol(handle, &guid, &interface, agent_handle, 0, EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL)
        mov rsp, [rsp+56]                 # deallocate stack
        pop rax                           # get interface
        ret

# rcx: handle
# rdx: &guid
# r8: agent_handle
close_protocol:
        push rsp                          # align stack to 16 bytes
        push [rsp]                        # align stack to 16 bytes
        and rsp, -16                      # align stack to 16 bytes
        xor r9, r9                        # arg4 = NULL
        sub rsp, 32                       # allocate shadow stack space for UEFI function
        call [r14+288]                    # system->boot->close_protocol(handle, &guid, agent_handle, 0)
        mov rsp, [rsp+40]                 # deallocate stack
        ret

# rdx: number of bytes to allocate
# r14: system->boot
# returns pointer in rax
allocate_pool:
        push rdx                          # allocate stack for pool pointer
        mov r8, rsp                       # arg3 = &pool
        push 2
        pop rcx                           # arg1 = EFI_LOADER_DATA
        push rsp                          # align stack to 16 bytes
        push [rsp]                        # align stack to 16 bytes
        and rsp, -16                      # align stack to 16 bytes
        sub rsp, 32                       # allocate shadow stack space for UEFI and align stack
        call [r14+64]                     # system->boot->allocate_pool(EFI_LOADER_DATA, 2048, &pool)
        mov rsp, [rsp+40]                 # deallocate stack
        pop rax                           # get pool
        ret

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

FILE_INFO_PROTOCOL:
.long  0x09576e92
.short 0x6d3f
.short 0x11d2
.byte  0x8e, 0x39, 0x00, 0xa0, 0xc9, 0x69, 0x72, 0x3b

default_file:
.byte 0x6B, 0, 0x61, 0, 0x65, 0, 0x6D, 0, 0x2E, 0
.byte 0x61, 0, 0x6D, 0, 0x64, 0, 0x36, 0, 0x34, 0, 0, 0      # L"kaem.amd64"

prefix:
.byte 0x20, 0, 0x2B, 0, 0x3E, 0, 0x20, 0, 0, 0               # L" +> "

subprocess_error:
.byte 0x53, 0, 0x75, 0, 0x62, 0, 0x70, 0, 0x72, 0, 0x6F, 0, 0x63, 0, 0x65, 0
.byte 0x73, 0, 0x73, 0, 0x20, 0, 0x65, 0, 0x72, 0, 0x72, 0, 0x6F, 0, 0x72, 0
suffix:
.byte 0x0A, 0, 0x0D, 0, 0, 0                                 # L"Subprocess error\n\r"

image_handle:
.long 0, 0

image:
.long 0, 0

rootdir:
.long 0, 0

root_device:
.long 0, 0

system_out:
.long 0, 0

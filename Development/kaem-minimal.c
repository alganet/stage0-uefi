/* SPDX-FileCopyrightText: 2022 Andrius Štikonas <andrius@stikonas.eu>
 *
 * SPDX-License-Identifier: GPL-3.0-or-later */

/* Written in a low level C that is close to assembly.
 * We skip error checking since this is a prototype for hex0 code */

#include "efi/efi.h"

#define max_string 512

#define HARDWARE_DEVICE_PATH 1
#define END_HARDWARE_DEVICE_PATH 0x7F
#define END_ENTIRE_DEVICE_PATH 0xFF
#define MEMORY_MAPPED 3

efi_status_t efi_main(efi_handle_t image_handle, struct efi_system_table *system)
{
    struct efi_loaded_image_protocol *image, *child_image;
    struct efi_simple_file_system_protocol *rootfs;
    struct efi_file_protocol *rootdir;
    struct efi_guid guid1 = EFI_LOADED_IMAGE_PROTOCOL_GUID;
    struct efi_guid guid2 = EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_GUID;
    struct efi_guid guid3 = EFI_FILE_INFO_GUID;

    system->boot->set_watchdog_timer(0, 0, 0, NULL);

    /* Open Loaded Image protocol */
    system->boot->open_protocol(image_handle, &guid1, (void **) &image, image_handle, 0,
                                EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL);

    /* Command line args */
    uint16_t *options = image->load_options;
    uint16_t default_file[] = L"kaem.amd64";
    uint16_t *script_file;
    do {
        ++options;
    } while (*options != ' ' && *options != 0); /* Skip app name */

    if (! *options) {
        script_file = default_file;
    }
    else {
        script_file = ++options;
    }

    /* Get root device */
    efi_handle_t root_device = image->device;
    system->boot->open_protocol(root_device, &guid2, (void **) &rootfs, image_handle, 0,
                                EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL);
    /* Get root fs */
    rootfs->open_volume(rootfs, &rootdir);

    /* Open file for reading */
    struct efi_file_protocol *fin;
    efi_status_t status = rootdir->open(rootdir, &fin, script_file, EFI_FILE_MODE_READ, EFI_FILE_READ_ONLY);
    if(status != EFI_SUCCESS) {
        return status;
    }

    uint16_t *command;
    system->boot->allocate_pool(EFI_LOADER_DATA, 2 * max_string, (void **) &command);

    unsigned int command_length = 0; /* length of command without arguments */
    unsigned int options_length = 0; /* length of command with arguments */
    unsigned int i;
    uint8_t c;
    efi_uint_t size = 1;
    efi_uint_t file_size = 1;
    efi_uint_t return_code;
    void *executable;
    efi_handle_t child_ih;

    do
    {
        i = 0;
        command_length = 0;
        do
        {
            fin->read(fin, &size, &c);
            if (size == 0) {
                rootdir->close(fin);
                system->boot->free_pool(command);
                return EFI_SUCCESS;
            }
            else if(c == '\n') {
                break;
            }
            else if (c == ' ' && command_length == 0) {
                command_length = i;
            }
            else if (c == '#') {
                /* Line comments */
                do {
                    fin->read(fin, &size, &c);
                } while (c != '\n');
                break;
            }
            command[i] = c;
            i++;
        } while(true);

        if (command_length == 0 ) {
            continue;
        }
        options_length = i;
        command[i] = 0;

        system->out->output_string(system->out, L" +> ");
        system->out->output_string(system->out, command);
        system->out->output_string(system->out, L"\r\n");

        command[command_length] = 0;

        /* Open executable file for reading and load it into memory */
        struct efi_file_protocol *fcmd;
        efi_status_t status = rootdir->open(rootdir, &fcmd, command, EFI_FILE_MODE_READ, EFI_FILE_READ_ONLY);
        if(status != EFI_SUCCESS) {
            system->boot->free_pool(command);
            rootdir->close(fin);
            return status;
        }

        struct efi_file_info *file_info;
        file_size = sizeof(struct efi_file_info);
        system->boot->allocate_pool(EFI_LOADER_DATA, file_size, (void **) &file_info);
        fcmd->get_info(fcmd, &guid3, &file_size, file_info);
        file_size = file_info->file_size;
        system->boot->free_pool(file_info);

        system->boot->allocate_pool(EFI_LOADER_CODE, file_size, (void **) &executable);
        fcmd->read(fcmd, &file_size, executable);

        struct efi_device_path_protocol *device_path;
        system->boot->allocate_pool(EFI_LOADER_DATA, 4 + sizeof(struct efi_device_path_protocol), (void **) &device_path);
        device_path->type = HARDWARE_DEVICE_PATH;
        device_path->subtype = MEMORY_MAPPED;
        device_path->length = sizeof(struct efi_device_path_protocol);
        device_path->memory_type = EFI_LOADER_CODE;
        device_path->start_address = (uint64_t) executable;
        device_path->end_address = (uint64_t) executable + file_size;
        device_path[1].type = END_HARDWARE_DEVICE_PATH;
        device_path[1].subtype = END_ENTIRE_DEVICE_PATH;
        device_path[1].length = 4;

        system->boot->load_image(0, image_handle, device_path, executable, file_size, &child_ih);
        system->boot->free_pool(device_path);
        system->boot->free_pool(executable);

        /* Deal with command line arguments */
        command[command_length] = ' ';
        system->boot->open_protocol(child_ih, &guid1, (void **) &child_image, child_ih, 0,
                                EFI_OPEN_PROTOCOL_BY_HANDLE_PROTOCOL);
        child_image->load_options = command;
        child_image->load_options_size = options_length;
        child_image->device = image->device;

        /* Run command */
        return_code = system->boot->start_image(child_ih, 0, 0);

        if(return_code != 0) {
            system->boot->free_pool(command);
            system->out->output_string(system->out, L"Subprocess error.\r\n");
            return return_code;
        }
    } while(true);
}

/* SPDX-License-Identifier: Unlicense */

#ifndef __EFI_DEVICE_PATH_PROTOCOL_H__
#define __EFI_DEVICE_PATH_PROTOCOL_H__

#include "types.h"

struct efi_device_path_protocol {
	uint8_t type;
	uint8_t subtype;
	uint16_t length;
	uint32_t memory_type;
	uint64_t start_address;
	uint64_t end_address;
};

#endif // __EFI_DEVICE_PATH_PROTOCOL_H__

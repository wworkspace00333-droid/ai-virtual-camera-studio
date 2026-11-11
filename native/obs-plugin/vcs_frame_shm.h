/*
 * Shared-memory frame transport between the OBS plugin (writer) and AI Virtual Camera Studio (reader).
 *
 * POSIX shared memory "/vcs-obs-frames": a 4 KiB header followed by VCS_SHM_SLOTS frame slots.
 * Triple buffering + per-slot sequence lock:
 *   writer:  n = latest + 1; slot = n % SLOTS
 *            slot.seq = 2n - 1   (odd: being written) -> copy pixels + metadata -> slot.seq = 2n
 *            latest = n          (release)
 *   reader:  n = latest (acquire); s = slot[n % SLOTS].seq; require s == 2n;
 *            copy; re-read seq; equal -> consistent frame, otherwise retry with the newest frame.
 * The writer never waits for the reader; a slow reader just gets the newest frame (no queueing).
 * Pixel format: BGRA 8-bit, full range, top-down rows of `stride` bytes.
 */
#pragma once
#include <stdatomic.h>
#include <stdint.h>

#define VCS_SHM_NAME "/vcs-obs-frames"
#define VCS_SHM_MAGIC 0x46534356u /* 'VCSF' little-endian */
#define VCS_SHM_VERSION 1u
#define VCS_SHM_SLOTS 3u
#define VCS_SHM_HEADER_BYTES 4096u
#define VCS_SHM_MAX_WIDTH 3840u
#define VCS_SHM_MAX_HEIGHT 2160u
#define VCS_SHM_SLOT_BYTES ((uint64_t)VCS_SHM_MAX_WIDTH * VCS_SHM_MAX_HEIGHT * 4u)
#define VCS_SHM_TOTAL_BYTES (VCS_SHM_HEADER_BYTES + VCS_SHM_SLOTS * VCS_SHM_SLOT_BYTES)
#define VCS_FMT_BGRA 1u

struct vcs_slot_info {
	_Atomic uint64_t seq;      /* 2n when frame n is complete, odd while being written */
	uint32_t width;
	uint32_t height;
	uint32_t stride;
	uint32_t format;
	uint64_t timestamp_ns;     /* OBS video timestamp (os_gettime_ns: mach_absolute_time in ns) */
};

struct vcs_shm_header {
	uint32_t magic;
	uint32_t version;
	uint32_t slots;
	uint32_t header_bytes;
	uint64_t slot_bytes;
	_Atomic uint64_t latest;        /* number of the newest complete frame (0 = none yet) */
	_Atomic uint64_t heartbeat_ns;  /* writer clock at the last frame */
	int32_t writer_pid;
	uint32_t fps_num;
	uint32_t fps_den;
	_Atomic uint32_t active;        /* 1 while the writer is publishing, 0 after it stopped */
	struct vcs_slot_info slot[VCS_SHM_SLOTS];
};

_Static_assert(sizeof(struct vcs_shm_header) <= VCS_SHM_HEADER_BYTES, "header too large");

static inline uint8_t *vcs_slot_pixels(void *base, uint32_t slot)
{
	return (uint8_t *)base + VCS_SHM_HEADER_BYTES + (uint64_t)slot * VCS_SHM_SLOT_BYTES;
}

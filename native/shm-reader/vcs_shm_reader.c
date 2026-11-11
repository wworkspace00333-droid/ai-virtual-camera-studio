/*
 * libVCSShm: reader (and test writer) for the OBS shared-memory frame transport (vcs_frame_shm.h).
 * Called from Python through ctypes, which releases the GIL for the duration of each call.
 */
#include <CoreVideo/CoreVideo.h>
#include <errno.h>
#include <fcntl.h>
#include <mach/mach_time.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "../obs-plugin/vcs_frame_shm.h"

typedef struct {
	int fd;
	void *base;
	struct vcs_shm_header *hdr;
	int writable;
} vcs_shm;

typedef struct {
	uint64_t frame;
	uint32_t width, height, stride, format;
	uint64_t timestamp_ns;
	uint32_t fps_num, fps_den;
	int32_t writer_pid;
	uint32_t active;
	uint64_t heartbeat_ns;
} vcs_frame_info;

static uint64_t now_ns(void)
{
	static mach_timebase_info_data_t tb;
	if (!tb.denom)
		mach_timebase_info(&tb);
	return mach_absolute_time() * tb.numer / tb.denom;   /* same clock as OBS os_gettime_ns on macOS */
}

/* status: 0 ok, -1 not found (OBS not sharing), -2 mmap/size error, -3 bad magic/version */
static vcs_shm *open_common(const char *name, int writable, int *status)
{
	int fd = shm_open(name ? name : VCS_SHM_NAME, writable ? (O_CREAT | O_RDWR) : O_RDONLY, 0600);
	if (fd < 0) {
		if (status) *status = -1;
		return NULL;
	}
	struct stat st;
	if (fstat(fd, &st) != 0) {
		close(fd);
		if (status) *status = -2;
		return NULL;
	}
	if (writable && st.st_size == 0 && ftruncate(fd, (off_t)VCS_SHM_TOTAL_BYTES) != 0) {
		close(fd);
		if (status) *status = -2;
		return NULL;
	}
	if (!writable && (uint64_t)st.st_size < VCS_SHM_TOTAL_BYTES) {
		close(fd);
		if (status) *status = -2;
		return NULL;
	}
	void *base = mmap(NULL, VCS_SHM_TOTAL_BYTES, writable ? PROT_READ | PROT_WRITE : PROT_READ, MAP_SHARED, fd, 0);
	if (base == MAP_FAILED) {
		close(fd);
		if (status) *status = -2;
		return NULL;
	}
	vcs_shm *s = calloc(1, sizeof(*s));
	s->fd = fd;
	s->base = base;
	s->hdr = base;
	s->writable = writable;
	if (!writable && (s->hdr->magic != VCS_SHM_MAGIC || s->hdr->version != VCS_SHM_VERSION)) {
		munmap(base, VCS_SHM_TOTAL_BYTES);
		close(fd);
		free(s);
		if (status) *status = -3;
		return NULL;
	}
	if (status) *status = 0;
	return s;
}

void *vcs_shm_reader_open(const char *name, int *status) { return open_common(name, 0, status); }

void vcs_shm_close(void *h)
{
	vcs_shm *s = h;
	if (!s) return;
	munmap(s->base, VCS_SHM_TOTAL_BYTES);
	close(s->fd);
	free(s);
}

void vcs_shm_info(void *h, vcs_frame_info *out)
{
	vcs_shm *s = h;
	memset(out, 0, sizeof(*out));
	if (!s) return;
	struct vcs_shm_header *hd = s->hdr;
	out->frame = atomic_load_explicit(&hd->latest, memory_order_acquire);
	out->fps_num = hd->fps_num;
	out->fps_den = hd->fps_den;
	out->writer_pid = hd->writer_pid;
	out->active = atomic_load_explicit(&hd->active, memory_order_acquire);
	out->heartbeat_ns = atomic_load_explicit(&hd->heartbeat_ns, memory_order_relaxed);
	if (out->frame) {
		struct vcs_slot_info *sl = &hd->slot[out->frame % VCS_SHM_SLOTS];
		out->width = sl->width; out->height = sl->height; out->stride = sl->stride; out->format = sl->format;
		out->timestamp_ns = sl->timestamp_ns;
	}
}

uint64_t vcs_shm_now_ns(void) { return now_ns(); }

/* Wait for a frame newer than `last` (polling every 250 us, GIL released). 0 new, 1 timeout, 2 writer inactive. */
int vcs_shm_wait(void *h, uint64_t last, int timeout_ms, uint64_t *frame_out)
{
	vcs_shm *s = h;
	uint64_t deadline = now_ns() + (uint64_t)timeout_ms * 1000000ull;
	struct timespec ts = {0, 250000};
	for (;;) {
		uint64_t n = atomic_load_explicit(&s->hdr->latest, memory_order_acquire);
		if (n > last) {
			if (frame_out) *frame_out = n;
			return 0;
		}
		if (!atomic_load_explicit(&s->hdr->active, memory_order_acquire))
			return 2;
		if (now_ns() >= deadline)
			return 1;
		nanosleep(&ts, NULL);
	}
}

/* Copy the newest consistent frame into a BGRA CVPixelBuffer of matching size.
 * Returns 0 ok, 1 size mismatch (info filled, nothing copied), 2 no frame, 3 torn (retry), <0 error. */
int vcs_shm_copy_to_pixelbuffer(void *h, void *pixel_buffer, vcs_frame_info *info)
{
	vcs_shm *s = h;
	CVPixelBufferRef pb = (CVPixelBufferRef)pixel_buffer;
	for (int attempt = 0; attempt < 4; attempt++) {
		uint64_t n = atomic_load_explicit(&s->hdr->latest, memory_order_acquire);
		if (!n) return 2;
		struct vcs_slot_info *sl = &s->hdr->slot[n % VCS_SHM_SLOTS];
		uint64_t s1 = atomic_load_explicit(&sl->seq, memory_order_acquire);
		if (s1 != 2 * n) continue;                 /* overtaken by the writer: take the newer frame */
		uint32_t w = sl->width, ht = sl->height, stride = sl->stride;
		uint64_t ts = sl->timestamp_ns;
		if (info) {
			vcs_shm_info(h, info);
			info->frame = n; info->width = w; info->height = ht; info->stride = stride; info->timestamp_ns = ts;
		}
		if (!pb) return 1;
		if ((uint32_t)CVPixelBufferGetWidth(pb) != w || (uint32_t)CVPixelBufferGetHeight(pb) != ht) return 1;
		if (CVPixelBufferLockBaseAddress(pb, 0) != kCVReturnSuccess) return -1;
		uint8_t *dst = CVPixelBufferGetBaseAddress(pb);
		size_t dstride = CVPixelBufferGetBytesPerRow(pb);
		const uint8_t *src = vcs_slot_pixels(s->base, (uint32_t)(n % VCS_SHM_SLOTS));
		if (dstride == stride) {
			memcpy(dst, src, (size_t)stride * ht);
		} else {
			for (uint32_t y = 0; y < ht; y++)
				memcpy(dst + y * dstride, src + (size_t)y * stride, (size_t)w * 4);
		}
		CVPixelBufferUnlockBaseAddress(pb, 0);
		atomic_thread_fence(memory_order_acquire);
		uint64_t s2 = atomic_load_explicit(&sl->seq, memory_order_acquire);
		if (s2 == s1) return 0;
	}
	return 3;
}

/* ---------------- test writer (same protocol as the OBS plugin) ---------------- */
void *vcs_shm_writer_open(const char *name, uint32_t fps_num, uint32_t fps_den, int *status)
{
	vcs_shm *s = open_common(name, 1, status);
	if (!s) return NULL;
	struct vcs_shm_header *hd = s->hdr;
	hd->magic = VCS_SHM_MAGIC; hd->version = VCS_SHM_VERSION; hd->slots = VCS_SHM_SLOTS;
	hd->header_bytes = VCS_SHM_HEADER_BYTES; hd->slot_bytes = VCS_SHM_SLOT_BYTES;
	hd->writer_pid = getpid(); hd->fps_num = fps_num; hd->fps_den = fps_den;
	atomic_store(&hd->active, 1);
	return s;
}

int vcs_shm_writer_publish(void *h, const uint8_t *bgra, uint32_t w, uint32_t ht, uint32_t stride, uint64_t ts_ns)
{
	vcs_shm *s = h;
	if (!s || !s->writable || w > VCS_SHM_MAX_WIDTH || ht > VCS_SHM_MAX_HEIGHT) return -1;
	struct vcs_shm_header *hd = s->hdr;
	uint64_t n = atomic_load_explicit(&hd->latest, memory_order_relaxed) + 1;
	uint32_t idx = (uint32_t)(n % VCS_SHM_SLOTS);
	struct vcs_slot_info *sl = &hd->slot[idx];
	atomic_store_explicit(&sl->seq, 2 * n - 1, memory_order_release);
	uint8_t *dst = vcs_slot_pixels(s->base, idx);
	for (uint32_t y = 0; y < ht; y++) memcpy(dst + (size_t)y * w * 4, bgra + (size_t)y * stride, (size_t)w * 4);
	sl->width = w; sl->height = ht; sl->stride = w * 4; sl->format = VCS_FMT_BGRA; sl->timestamp_ns = ts_ns ? ts_ns : now_ns();
	atomic_store_explicit(&sl->seq, 2 * n, memory_order_release);
	atomic_store_explicit(&hd->latest, n, memory_order_release);
	atomic_store_explicit(&hd->heartbeat_ns, now_ns(), memory_order_relaxed);
	return 0;
}

void vcs_shm_writer_set_active(void *h, int active)
{
	vcs_shm *s = h;
	if (s && s->writable) atomic_store(&s->hdr->active, active ? 1u : 0u);
}

int vcs_shm_unlink(const char *name) { return shm_unlink(name ? name : VCS_SHM_NAME); }

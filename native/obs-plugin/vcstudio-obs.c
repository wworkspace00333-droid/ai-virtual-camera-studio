/*
 * vcstudio-obs: OBS Studio plugin that shares OBS's final program output with AI Virtual Camera Studio
 * through shared memory (see vcs_frame_shm.h). Frames are converted to BGRA by OBS's own GPU/CPU
 * conversion path (obs_add_raw_video_callback) at the output resolution.
 *
 * Tools menu: "AI Virtual Camera Studio: share frames (on/off)". The on/off state is remembered
 * (plugin config "config.json", key "enabled") and restored when OBS starts.
 */
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <obs-frontend-api.h>
#include <obs-module.h>
#include <util/platform.h>

#include "vcs_frame_shm.h"

OBS_DECLARE_MODULE()
MODULE_EXPORT const char *obs_module_description(void)
{
	return "Shares the OBS program output with AI Virtual Camera Studio (shared memory, BGRA)";
}

struct vcs_share {
	int fd;
	void *base;
	struct vcs_shm_header *hdr;
	bool running;
	uint32_t width, height;
	uint64_t frames, skipped;
};
static struct vcs_share g = {.fd = -1};

static void raw_video(void *param, struct video_data *frame)
{
	struct vcs_share *s = param;
	if (!s->hdr || !frame->data[0])
		return;
	struct vcs_shm_header *h = s->hdr;
	uint64_t n = atomic_load_explicit(&h->latest, memory_order_relaxed) + 1;
	uint32_t idx = (uint32_t)(n % VCS_SHM_SLOTS);
	struct vcs_slot_info *slot = &h->slot[idx];
	uint32_t row = s->width * 4;
	atomic_store_explicit(&slot->seq, 2 * n - 1, memory_order_release);
	uint8_t *dst = vcs_slot_pixels(s->base, idx);
	const uint8_t *src = frame->data[0];
	if (frame->linesize[0] == row) {
		memcpy(dst, src, (size_t)row * s->height);
	} else {
		for (uint32_t y = 0; y < s->height; y++)
			memcpy(dst + (size_t)y * row, src + (size_t)y * frame->linesize[0], row);
	}
	slot->width = s->width;
	slot->height = s->height;
	slot->stride = row;
	slot->format = VCS_FMT_BGRA;
	slot->timestamp_ns = frame->timestamp;
	atomic_store_explicit(&slot->seq, 2 * n, memory_order_release);
	atomic_store_explicit(&h->latest, n, memory_order_release);
	atomic_store_explicit(&h->heartbeat_ns, os_gettime_ns(), memory_order_relaxed);
	s->frames++;
}

static bool share_start(void)
{
	if (g.running)
		return true;
	struct obs_video_info ovi;
	if (!obs_get_video_info(&ovi)) {
		blog(LOG_WARNING, "[vcstudio-obs] video not initialised yet");
		return false;
	}
	if (ovi.output_width > VCS_SHM_MAX_WIDTH || ovi.output_height > VCS_SHM_MAX_HEIGHT) {
		blog(LOG_ERROR, "[vcstudio-obs] output %ux%u exceeds %ux%u", ovi.output_width, ovi.output_height,
		     VCS_SHM_MAX_WIDTH, VCS_SHM_MAX_HEIGHT);
		return false;
	}
	g.fd = shm_open(VCS_SHM_NAME, O_CREAT | O_RDWR, 0600);
	if (g.fd < 0) {
		blog(LOG_ERROR, "[vcstudio-obs] shm_open failed: %s", strerror(errno));
		return false;
	}
	struct stat st;
	if (fstat(g.fd, &st) == 0 && (uint64_t)st.st_size != VCS_SHM_TOTAL_BYTES) {
		if (st.st_size == 0 && ftruncate(g.fd, (off_t)VCS_SHM_TOTAL_BYTES) != 0) {
			blog(LOG_ERROR, "[vcstudio-obs] ftruncate failed: %s", strerror(errno));
			close(g.fd);
			g.fd = -1;
			return false;
		}
	}
	g.base = mmap(NULL, VCS_SHM_TOTAL_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, g.fd, 0);
	if (g.base == MAP_FAILED) {
		blog(LOG_ERROR, "[vcstudio-obs] mmap failed: %s", strerror(errno));
		close(g.fd);
		g.fd = -1;
		g.base = NULL;
		return false;
	}
	g.hdr = g.base;
	g.hdr->magic = VCS_SHM_MAGIC;
	g.hdr->version = VCS_SHM_VERSION;
	g.hdr->slots = VCS_SHM_SLOTS;
	g.hdr->header_bytes = VCS_SHM_HEADER_BYTES;
	g.hdr->slot_bytes = VCS_SHM_SLOT_BYTES;
	g.hdr->writer_pid = getpid();
	g.hdr->fps_num = ovi.fps_num;
	g.hdr->fps_den = ovi.fps_den;
	g.width = ovi.output_width;
	g.height = ovi.output_height;
	struct video_scale_info conv = {
		.format = VIDEO_FORMAT_BGRA,
		.width = ovi.output_width,
		.height = ovi.output_height,
		.range = VIDEO_RANGE_FULL,
		.colorspace = VIDEO_CS_DEFAULT,
	};
	atomic_store(&g.hdr->active, 1);
	obs_add_raw_video_callback(&conv, raw_video, &g);
	g.running = true;
	blog(LOG_INFO, "[vcstudio-obs] sharing %ux%u @ %u/%u fps to %s", g.width, g.height, ovi.fps_num, ovi.fps_den,
	     VCS_SHM_NAME);
	return true;
}

static void share_stop(void)
{
	if (!g.running)
		return;
	obs_remove_raw_video_callback(raw_video, &g);
	atomic_store(&g.hdr->active, 0);
	munmap(g.base, VCS_SHM_TOTAL_BYTES);
	close(g.fd);
	g.fd = -1;
	g.base = NULL;
	g.hdr = NULL;
	g.running = false;
	blog(LOG_INFO, "[vcstudio-obs] stopped after %llu frames", (unsigned long long)g.frames);
}

static void save_enabled(bool enabled)
{
	char *dir = obs_module_config_path("");
	if (dir) {
		os_mkdirs(dir);
		bfree(dir);
	}
	char *path = obs_module_config_path("config.json");
	if (!path)
		return;
	obs_data_t *d = obs_data_create();
	obs_data_set_bool(d, "enabled", enabled);
	obs_data_save_json_safe(d, path, "tmp", "bak");
	obs_data_release(d);
	bfree(path);
}

static bool load_enabled(void)
{
	char *path = obs_module_config_path("config.json");
	bool enabled = false;
	if (path) {
		obs_data_t *d = obs_data_create_from_json_file_safe(path, "bak");
		if (d) {
			enabled = obs_data_get_bool(d, "enabled");
			obs_data_release(d);
		}
		bfree(path);
	}
	return enabled;
}

static void toggle(void *unused)
{
	(void)unused;
	if (g.running) {
		share_stop();
		save_enabled(false);
	} else if (share_start()) {
		save_enabled(true);
	}
}

static void on_frontend_event(enum obs_frontend_event event, void *unused)
{
	(void)unused;
	if (event == OBS_FRONTEND_EVENT_FINISHED_LOADING && load_enabled())
		share_start();
	else if (event == OBS_FRONTEND_EVENT_EXIT)
		share_stop();
}

bool obs_module_load(void)
{
	obs_frontend_add_tools_menu_item("AI Virtual Camera Studio: share frames (on/off)", toggle, NULL);
	obs_frontend_add_event_callback(on_frontend_event, NULL);
	blog(LOG_INFO, "[vcstudio-obs] loaded (protocol v%u)", VCS_SHM_VERSION);
	return true;
}

void obs_module_unload(void)
{
	share_stop();
}

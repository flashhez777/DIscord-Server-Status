#ifndef DISCORD_GLOBAL_STATUS_MONITOR_H
#define DISCORD_GLOBAL_STATUS_MONITOR_H

#ifdef _WIN32
#  if defined(DGSM_BUILD_DLL)
#    define DGSM_API __declspec(dllexport)
#  else
#    define DGSM_API __declspec(dllimport)
#  endif
#else
#  define DGSM_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Returns the number of bytes written, excluding the trailing NUL. */
DGSM_API int dgsm_get_version(char *buffer, unsigned int buffer_size);
DGSM_API int dgsm_get_last_error(char *buffer, unsigned int buffer_size);

/* Runs one monitor cycle or starts continuous monitoring. */
DGSM_API int dgsm_run_once(void);
DGSM_API int dgsm_run_monitor(void);

#ifdef __cplusplus
}
#endif
#endif

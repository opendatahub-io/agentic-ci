/*
 * agentic-ci-sandbox-setup: run a command as a child process and wait for it.
 *
 * Usage: agentic-ci-sandbox-setup COMMAND [ARG...]
 *
 * OpenShell decides which network rules apply to a connection from the
 * caller's executable (/proc/<pid>/exe) and the executables of its parent
 * chain. agentic-ci binds setup and validate egress to this binary's path, so
 * those rules apply to the command it runs and everything that command
 * starts, and to nothing else. That only holds while this process stays in
 * the parent chain: it forks, runs the command in the child and waits. It
 * never replaces itself with exec, because after an exec it would no longer
 * be the executable or an ancestor.
 *
 * - The command runs in its own process group. SIGTERM, SIGINT, SIGHUP and
 *   SIGQUIT sent to the shim are forwarded to that whole group, so a step
 *   killed on timeout takes the processes it started (bash -> npm -> node)
 *   with it. A signal the shim was started with ignored stays ignored, and
 *   the child inherits that. Because of the separate group the shim is meant
 *   for non-interactive steps: on a terminal the command would not be in the
 *   foreground group.
 * - The environment, working directory and file descriptors are passed
 *   through unchanged.
 * - Exit status: the child's exit status, or 128+N when signal N killed it;
 *   126 or 127 when the command cannot be run (as in a shell); 2 without a
 *   command; 125 when the shim itself fails (signal setup, fork or wait).
 *
 * Built statically in the sandbox images, so it needs no shared library.
 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define NAME "agentic-ci-sandbox-setup"
#define EXIT_USAGE 2
#define EXIT_SHIM_FAILURE 125
#define EXIT_CANNOT_EXECUTE 126
#define EXIT_NOT_FOUND 127

static const int forwarded[] = {SIGTERM, SIGINT, SIGHUP, SIGQUIT};
#define N_FORWARDED (sizeof(forwarded) / sizeof(forwarded[0]))

/*
 * The child's pid (and process group) while it runs; 0 before fork returns
 * and again once the child has been reaped, so a late signal can never reach
 * a reused pid.
 */
static volatile sig_atomic_t child_pid = 0;

static void forward(int sig)
{
    int saved_errno = errno;

    /* Guard against kill(0, ...), which would signal the shim's own group. */
    if (child_pid > 0)
        kill(-(pid_t)child_pid, sig);
    errno = saved_errno;
}

static int shim_failure(const char *what)
{
    fprintf(stderr, NAME ": %s failed: %s\n", what, strerror(errno));
    return EXIT_SHIM_FAILURE;
}

int main(int argc, char **argv)
{
    sigset_t block, previous;
    struct sigaction action, old;
    int installed[N_FORWARDED] = {0};
    size_t i;
    pid_t pid;
    int status;
    int err;

    if (argc < 2) {
        fprintf(stderr, "usage: " NAME " COMMAND [ARG...]\n");
        return EXIT_USAGE;
    }

    /*
     * Block the forwarded signals until the child's pid is known, so none
     * arrives while there is no child to forward it to. A signal sent in
     * that window stays pending and is forwarded once they are unblocked.
     */
    sigemptyset(&block);
    for (i = 0; i < N_FORWARDED; i++)
        sigaddset(&block, forwarded[i]);
    if (sigprocmask(SIG_BLOCK, &block, &previous) != 0)
        return shim_failure("sigprocmask");

    memset(&action, 0, sizeof(action));
    action.sa_handler = forward;
    sigemptyset(&action.sa_mask);
    for (i = 0; i < N_FORWARDED; i++) {
        if (sigaction(forwarded[i], NULL, &old) != 0)
            return shim_failure("sigaction");
        if (old.sa_handler == SIG_IGN)
            continue;
        if (sigaction(forwarded[i], &action, NULL) != 0)
            return shim_failure("sigaction");
        installed[i] = 1;
    }

    pid = fork();
    if (pid < 0)
        return shim_failure("fork");
    if (pid == 0) {
        /*
         * Child: lead a new process group, restore the default handlers and
         * the signal mask, then run. The parent sets the group too, so it
         * exists whichever of the two runs first.
         */
        setpgid(0, 0);
        for (i = 0; i < N_FORWARDED; i++) {
            if (installed[i])
                signal(forwarded[i], SIG_DFL);
        }
        sigprocmask(SIG_SETMASK, &previous, NULL);
        execvp(argv[1], &argv[1]);
        err = errno;
        fprintf(stderr, NAME ": cannot run %s: %s\n", argv[1], strerror(err));
        _exit(err == ENOENT ? EXIT_NOT_FOUND : EXIT_CANNOT_EXECUTE);
    }

    /* EACCES: the child already ran exec after its own setpgid, which is fine. */
    if (setpgid(pid, pid) != 0 && errno != EACCES)
        return shim_failure("setpgid");
    child_pid = pid;
    if (sigprocmask(SIG_SETMASK, &previous, NULL) != 0)
        return shim_failure("sigprocmask");

    /* A forwarded signal interrupts waitpid with EINTR; keep waiting. */
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR)
            return shim_failure("wait");
    }

    /* The pid is free for reuse now: stop forwarding before forgetting it. */
    if (sigprocmask(SIG_BLOCK, &block, NULL) != 0)
        return shim_failure("sigprocmask");
    child_pid = 0;

    if (WIFEXITED(status))
        return WEXITSTATUS(status);
    if (WIFSIGNALED(status))
        return 128 + WTERMSIG(status);
    return EXIT_SHIM_FAILURE;
}

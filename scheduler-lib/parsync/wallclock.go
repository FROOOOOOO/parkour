package parsync

import "time"

// NextWallClockTick returns the next tick time aligned to the wall clock so
// that independent processes observe the same global boundaries.
// The boundary is `floor(now.UnixNano()/interval) * interval + interval`.
//
// Using this instead of time.NewTicker(interval) removes the dependency on
// each process's startup time: two Schedulers booted seconds apart will still
// fire rotation ticks at the same absolute instants, preserving ParSync's
// staggered-rotation semantics across the cluster.
//
// If interval <= 0, returns now unchanged.
func NextWallClockTick(now time.Time, interval time.Duration) time.Time {
	period := interval.Nanoseconds()
	if period <= 0 {
		return now
	}
	nsec := now.UnixNano()
	boundary := (nsec / period) * period
	return time.Unix(0, boundary+period)
}

// WallClockSlot returns the monotonically-increasing slot index for time t,
// computed as `floor(t.UnixNano()/interval)`. Two processes at the same wall
// clock instant observe the same slot index (up to NTP skew).
//
// Use as `partitionOrder[int(slot) % len(partitionOrder)]` to select the
// partition a scheduler should sync at the current slot.
//
// If interval <= 0, returns 0.
func WallClockSlot(t time.Time, interval time.Duration) int64 {
	period := interval.Nanoseconds()
	if period <= 0 {
		return 0
	}
	return t.UnixNano() / period
}

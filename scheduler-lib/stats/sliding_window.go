package stats

import (
	"sync"
)

// SlidingWindow is a fixed-size ring-buffer that tracks boolean outcomes and
// computes a rolling success rate.
type SlidingWindow struct {
	mu        sync.RWMutex
	data      []bool // true=success, false=failure
	size      int
	head      int // index of the next write position
	count     int // number of elements currently stored
	trueCount int // number of successful outcomes
}

func NewSlidingWindow(size int) *SlidingWindow {
	if size <= 0 {
		size = 100
	}
	return &SlidingWindow{
		data: make([]bool, size),
		size: size,
	}
}

// Add records a single outcome.
func (sw *SlidingWindow) Add(success bool) {
	sw.mu.Lock()
	defer sw.mu.Unlock()

	// If the window is full, subtract the value about to be overwritten.
	if sw.count == sw.size {
		if sw.data[sw.head] {
			sw.trueCount--
		}
	} else {
		sw.count++
	}

	// Write the new value.
	sw.data[sw.head] = success
	if success {
		sw.trueCount++
	}

	// Advance the write pointer.
	sw.head = (sw.head + 1) % sw.size
}

// SuccessRate returns the fraction of successful outcomes in the window.
// Returns 0.5 when the window is empty (neutral value).
func (sw *SlidingWindow) SuccessRate() float64 {
	sw.mu.RLock()
	defer sw.mu.RUnlock()

	if sw.count == 0 {
		return 0.5 // neutral value when no data
	}

	return float64(sw.trueCount) / float64(sw.count)
}

// Count returns the number of samples currently in the window.
func (sw *SlidingWindow) Count() int {
	sw.mu.RLock()
	defer sw.mu.RUnlock()
	return sw.count
}

// Reset clears all samples.
func (sw *SlidingWindow) Reset() {
	sw.mu.Lock()
	defer sw.mu.Unlock()

	sw.head = 0
	sw.count = 0
	sw.trueCount = 0
}

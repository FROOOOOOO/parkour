package stats

import (
	"sync"
)

// SlidingWindow 滑动窗口
type SlidingWindow struct {
	mu        sync.RWMutex
	data      []bool // true=成功, false=失败
	size      int
	head      int // 下一个写入位置
	count     int // 当前元素数量
	trueCount int // 成功计数
}

// NewSlidingWindow 创建滑动窗口
func NewSlidingWindow(size int) *SlidingWindow {
	if size <= 0 {
		size = 100
	}
	return &SlidingWindow{
		data: make([]bool, size),
		size: size,
	}
}

// Add 添加一个结果
func (sw *SlidingWindow) Add(success bool) {
	sw.mu.Lock()
	defer sw.mu.Unlock()

	// 如果窗口已满，需要减去被覆盖的值
	if sw.count == sw.size {
		if sw.data[sw.head] {
			sw.trueCount--
		}
	} else {
		sw.count++
	}

	// 写入新值
	sw.data[sw.head] = success
	if success {
		sw.trueCount++
	}

	// 移动头指针
	sw.head = (sw.head + 1) % sw.size
}

// SuccessRate 获取成功率
func (sw *SlidingWindow) SuccessRate() float64 {
	sw.mu.RLock()
	defer sw.mu.RUnlock()

	if sw.count == 0 {
		return 0.5 // 无数据时返回中性值
	}

	return float64(sw.trueCount) / float64(sw.count)
}

// Count 获取当前样本数
func (sw *SlidingWindow) Count() int {
	sw.mu.RLock()
	defer sw.mu.RUnlock()
	return sw.count
}

// Reset 重置
func (sw *SlidingWindow) Reset() {
	sw.mu.Lock()
	defer sw.mu.Unlock()

	sw.head = 0
	sw.count = 0
	sw.trueCount = 0
}

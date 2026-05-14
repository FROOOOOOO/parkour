// scheduler-lib/types/binding.go
package types

import "time"

// BindingResult 绑定结果
type BindingResult struct {
	PodKey       string    // Pod 标识
	NodeName     string    // 最终绑定的节点
	Rank         int       // 采纳的候选排名 (-1 表示全部失败)
	Success      bool      // 是否成功
	Timestamp    time.Time // 绑定时间
	SchedulerID  int       // 调度器 ID
	PartitionID  int       // 节点所属分区
	AttemptCount int       // 尝试次数
	ErrorMsg     string    // 失败原因
}

// IsFirstChoice 是否采纳首选
func (br *BindingResult) IsFirstChoice() bool {
	return br.Success && br.Rank == 0
}

// IsBackupChoice 是否采纳备选
func (br *BindingResult) IsBackupChoice() bool {
	return br.Success && br.Rank > 0
}

// IsAllFailed 是否全部失败
func (br *BindingResult) IsAllFailed() bool {
	return !br.Success
}

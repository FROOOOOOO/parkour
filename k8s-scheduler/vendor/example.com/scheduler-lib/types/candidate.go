package types

import "time"

// NodeScore 节点得分（从 scheduler 获取）
type NodeScore struct {
	NodeName string
	Score    int64
}

// CandidateNode 候选节点（selector 输出）
type CandidateNode struct {
	NodeScore
	Rank         int     // 节点排名（0=首选，1=备选1，...）
	ConflictRate float64 // 节点近期冲突率 [0,1]；由 stats provider 提供，参与打分策略
	PartitionID  int     // 所属分区
	Freshness    float64 // 新鲜度 [0,1]，1表示刚同步
	Reason       string  // 选择原因（用于调试）
}

// CandidateList 候选节点列表
type CandidateList struct {
	Candidates    []CandidateNode
	PodKey        string
	SchedulerID   int
	SelectionTime time.Time
}

// GetPrimary 获取首选节点
func (cl *CandidateList) GetPrimary() *CandidateNode {
	if len(cl.Candidates) == 0 {
		return nil
	}
	return &cl.Candidates[0]
}

// GetBackups 获取备选节点列表
func (cl *CandidateList) GetBackups() []CandidateNode {
	if len(cl.Candidates) <= 1 {
		return nil
	}
	return cl.Candidates[1:]
}

// NodeNames 获取所有节点名称
func (cl *CandidateList) NodeNames() []string {
	names := make([]string, len(cl.Candidates))
	for i, candidate := range cl.Candidates {
		names[i] = candidate.NodeName
	}
	return names
}

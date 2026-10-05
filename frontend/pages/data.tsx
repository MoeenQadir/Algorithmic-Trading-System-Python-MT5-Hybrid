import { useState, useEffect } from 'react'
import { Table, Select, DatePicker, Button, message } from 'antd'

const symbols = [
    { value: 'XAUUSD', label: 'XAUUSD' },
    { value: 'EURUSD', label: 'EURUSD' },
    { value: 'GBPUSD', label: 'GBPUSD' },
]

const timeframes = [
    { value: '1m', label: '1m' },
    { value: '5m', label: '5m' },
    { value: '15m', label: '15m' },
    { value: '1h', label: '1h' },
    { value: '4h', label: '4h' },
    { value: '1d', label: '1d' },
]

export default function DataManager() {
    const [inventory, setInventory] = useState([])
    const [loading, setLoading] = useState(false)
    const [selectedSymbol, setSelectedSymbol] = useState('XAUUSD')
    const [selectedTimeframe, setSelectedTimeframe] = useState('1h')
    const [dateRange, setDateRange] = useState([])

    const fetchInventory = async () => {
        setLoading(true)
        try {
            const res = await fetch('/api/data/inventory')
            setInventory(await res.json())
        } finally {
            setLoading(false)
        }
    }

    const downloadData = async () => {
        // Implementation for downloading data
    }

    useEffect(() => { fetchInventory() }, [])

    return (
        <div className="p-4">
            <div className="mb-4 flex gap-4">
                <Select options={symbols} value={selectedSymbol} onChange={setSelectedSymbol} />
                <Select options={timeframes} value={selectedTimeframe} onChange={setSelectedTimeframe} />
                <DatePicker.RangePicker value={dateRange} onChange={setDateRange} />
                <Button type="primary" onClick={downloadData}>Download Data</Button>
            </div>
            <Table
                columns={[
                    { title: 'Symbol', dataIndex: 'symbol' },
                    { title: 'Timeframe', dataIndex: 'timeframe' },
                    { title: 'Count', dataIndex: 'count' },
                    { title: 'From', dataIndex: 'from' },
                    { title: 'To', dataIndex: 'to' }
                ]}
                dataSource={inventory}
            />
        </div>
    )
}

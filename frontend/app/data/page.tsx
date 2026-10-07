'use client';

import { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { apiGet, apiPost, apiDelete } from '@/lib/api';

export default function DataManager() {
  const [inventory, setInventory] = useState<Array<any>>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [downloadSymbol, setDownloadSymbol] = useState<string>('XAUUSD');
  const [downloadTimeframe, setDownloadTimeframe] = useState<string>('15m');
  const [startDate, setStartDate] = useState<string>('');
  const [endDate, setEndDate] = useState<string>('');
  const [isDownloading, setIsDownloading] = useState<boolean>(false);
  const [downloadProgress, setDownloadProgress] = useState<number>(0);
  const [downloadMessage, setDownloadMessage] = useState<string>('');
  const [deleteMessage, setDeleteMessage] = useState<string>('');
  const [isDeleting, setIsDeleting] = useState<boolean>(false);

  const router = useRouter();

  useEffect(() => {
    fetchInventory();
  }, []);

  const handleDelete = async (symbol: string, timeframe: string) => {
    if (!symbol || !timeframe) {
      setDeleteMessage('Invalid symbol or timeframe');
      return;
    }

    setIsDeleting(true);
    setDeleteMessage('Deleting data...');

    try {
      // Call the backend DELETE endpoint with query parameters
      await apiDelete(`/data/delete?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`);

      setDeleteMessage(`Deleted data for ${symbol} ${timeframe}`);

      // Refresh inventory
      setTimeout(fetchInventory, 1000);
    } catch (err) {
      setDeleteMessage(err instanceof Error ? err.message : 'Unknown error');
      console.error('Error deleting data:', err);
    } finally {
      setIsDeleting(false);
    }
  };

  const fetchInventory = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiGet<{ data: Array<any> }>('/data/inventory');
      setInventory(data.data);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
      console.error('Error fetching inventory:', err);
    } finally {
      setLoading(false);
    }
  };

  const handleDownload = async () => {
    if (!downloadSymbol || !downloadTimeframe) {
      setDownloadMessage('Please select symbol and timeframe');
      return;
    }

    setIsDownloading(true);
    setDownloadProgress(0);
    setDownloadMessage('Starting download...');

    try {
      // Determine what to send: if both dates provided, use historical data path with no limit
      // If no dates provided, use recent data path with high limit
      // If only one date provided, treat as no dates (recent data path) to avoid confusion
      let limit = undefined;
      let start_date: string | undefined = undefined;
      let end_date: string | undefined = undefined;
      
      if (startDate && endDate) {
        // Both dates provided - use historical data path
        start_date = startDate;
        end_date = endDate;
      } else {
        // No dates or only one date provided - use recent data path with high limit
        limit = 1000000; // 1 million candles
      }

      const result = await apiPost<{ status: string; symbol: string; timeframe: string; candles_downloaded: number; message: string }>(
        '/data/download',
        {
          symbol: downloadSymbol,
          timeframe: downloadTimeframe,
          start_date: start_date,
          end_date: end_date,
          limit: limit
        }
      );
      setDownloadMessage(result.message || 'Download completed successfully');
      setDownloadProgress(100);

      // Refresh inventory after download
      setTimeout(fetchInventory, 1000);
    } catch (err) {
      setDownloadMessage(err instanceof Error ? err.message : 'Unknown error');
      console.error('Error downloading data:', err);
    } finally {
      setIsDownloading(false);
    }
  };

  const symbols = ['XAUUSD', 'EURUSD', 'GBPUSD', 'US100', 'US500', 'BTCUSD', 'ETHUSD'];
  const timeframes = ['1m', '5m', '15m', '30m', '1h', '4h', '1d'];

  return (
    <div className="min-h-screen bg-gray-50 p-8">
      <div className="max-w-7xl mx-auto">
        <h1 className="text-3xl font-bold text-gray-900 mb-6">Data Manager</h1>
        
        {/* Error Message */}
        {error && (
          <div className="bg-red-50 border-l-4 border-red-500 p-4 mb-6">
            <p className="text-red-700">{error}</p>
          </div>
        )}

        {/* Download Message */}
        {downloadMessage && (
          <div className={`
            bg-blue-50 border-l-4 border-blue-500 p-4 mb-6
            ${isDownloading ? 'animate-pulse' : ''}
          `}>
            <p className="text-blue-700">{downloadMessage}</p>
          </div>
        )}

        {/* Inventory Loading State */}
        {loading && !inventory.length && (
          <div className="text-center py-12">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500 mx-auto mb-4"></div>
            <p className="text-gray-500">Loading data inventory...</p>
          </div>
        )}

        {/* Inventory Table */}
        {!loading && (
          <div className="mb-8">
            <h2 className="text-2xl font-semibold text-gray-800 mb-4">Available Data Inventory</h2>
            <div className="overflow-x-auto bg-white rounded-lg shadow">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Symbol
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Timeframe
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Total Candles
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Earliest Timestamp
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Latest Timestamp
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Status
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Age (minutes)
                    </th>
                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                      Actions
                    </th>
                  </tr>
                </thead>
                 <tbody className="bg-white divide-y divide-gray-200">
                   {inventory.map((item, index) => (
                     <tr key={index} className={item.status === 'FRESH' ? 'bg-green-50' : item.status === 'STALE' ? 'bg-yellow-50' : item.status === 'ANCIENT' ? 'bg-red-50' : ''}>
                       <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">{item.symbol}</td>
                       <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">{item.timeframe}</td>
                       <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">{item.total_candles.toLocaleString()}</td>
                       <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                         {item.earliest_timestamp ? new Date(item.earliest_timestamp).toLocaleString() : 'N/A'}
                       </td>
                       <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                         {item.latest_timestamp ? new Date(item.latest_timestamp).toLocaleString() : 'N/A'}
                       </td>
                       <td className="px-6 py-4 whitespace-nowrap text-sm font-medium">
                         <span className={`
                           px-2 inline-flex text-xs leading-5 font-semibold rounded-full
                           ${item.status === 'FRESH' ? 'bg-green-100 text-green-800' :
                             item.status === 'STALE' ? 'bg-yellow-100 text-yellow-800' :
                             item.status === 'ANCIENT' ? 'bg-red-100 text-red-800' :
                             'bg-gray-100 text-gray-800'
                           }
                         `}>
                           {item.status}
                         </span>
                       </td>
                       <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">
                         {item.age_minutes !== null ? item.age_minutes.toString() : 'N/A'}
                       </td>
                       <td className="px-6 py-4 whitespace-nowrap text-sm text-center">
                         <button
                           onClick={() => handleDelete(item.symbol, item.timeframe)}
                           disabled={isDeleting}
                           className={`
                             bg-red-500 hover:bg-red-600 text-white font-medium py-1 px-2 rounded
                             disabled:opacity-50 disabled:cursor-not-allowed
                             transition-colors
                             ${isDeleting ? 'animate-pulse' : ''}
                           `}
                         >
                           Delete
                         </button>
                       </td>
                     </tr>
                   ))}
                  {inventory.length === 0 && (
                    <tr>
                      <td className="px-6 py-4 text-center text-gray-500" colSpan="7">
                        No data available. Use the downloader below to fetch data.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Download Controls */}
        <div className="bg-white rounded-lg shadow p-6">
          <h2 className="text-2xl font-semibold text-gray-800 mb-6">Download Market Data</h2>
          <form onSubmit={(e) => {
            e.preventDefault();
            handleDownload();
          }} className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Symbol</label>
                <select
                  value={downloadSymbol}
                  onChange={(e) => setDownloadSymbol(e.target.value)}
                  className="w-full px-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  {symbols.map(symbol => (
                    <option key={symbol} value={symbol}>
                      {symbol}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Timeframe</label>
                <select
                  value={downloadTimeframe}
                  onChange={(e) => setDownloadTimeframe(e.target.value)}
                  className="w-full px-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  {timeframes.map(tf => (
                    <option key={tf} value={tf}>
                      {tf}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">Start Date (Optional)</label>
                <input
                  type="date"
                  value={startDate}
                  onChange={(e) => setStartDate(e.target.value)}
                  className="w-full px-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-2">End Date (Optional)</label>
                <input
                  type="date"
                  value={endDate}
                  onChange={(e) => setEndDate(e.target.value)}
                  className="w-full px-4 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
            </div>
            
            <div className="flex items-center justify-between">
              <button
                type="submit"
                disabled={isDownloading}
                className={`
                  bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-4 rounded-md
                  disabled:opacity-50 disabled:cursor-not-allowed
                  transition-colors
                  ${isDownloading ? 'animate-pulse' : ''}
                `}
              >
                {isDownloading ? 'Downloading...' : 'Download MT5 Data'}
              </button>
              
              {isDownloading && (
                <div className="flex-1 mx-4">
                  <div className="w-full bg-gray-200 rounded-full h-2.5">
                    <div className="bg-blue-600 h-2.5 rounded-full" style={{ width: downloadProgress + '%' }}></div>
                  </div>
                  <p className="text-sm text-gray-600 mt-1">{downloadProgress}%</p>
                </div>
              )}
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
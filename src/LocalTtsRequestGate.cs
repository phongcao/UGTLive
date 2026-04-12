using System;
using System.Collections.Generic;

namespace UGTLive
{
    internal static class LocalTtsRequestGate
    {
        private sealed class Lease : IDisposable
        {
            private readonly string _serviceName;
            private bool _disposed;

            public Lease(string serviceName)
            {
                _serviceName = serviceName;
            }

            public void Dispose()
            {
                if (_disposed)
                {
                    return;
                }

                _disposed = true;
                Release(_serviceName);
            }
        }

        private static readonly object GateLock = new object();
        private static readonly HashSet<string> BusyServices = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        public static IDisposable? TryAcquire(string serviceName)
        {
            lock (GateLock)
            {
                if (BusyServices.Contains(serviceName))
                {
                    return null;
                }

                BusyServices.Add(serviceName);
                return new Lease(serviceName);
            }
        }

        private static void Release(string serviceName)
        {
            lock (GateLock)
            {
                BusyServices.Remove(serviceName);
            }
        }
    }
}
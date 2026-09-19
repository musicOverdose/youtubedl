const API = {
  async request(endpoint, options = {}) {
    options.headers = options.headers || {};
    if (!(options.body instanceof FormData) && !options.headers['Content-Type']) {
      options.headers['Content-Type'] = 'application/json';
    }

    try {
      const response = await fetch(endpoint, options);
      if (response.status === 401 && !endpoint.includes('/api/auth/login')) {
        document.getElementById('login-modal').classList.add('active');
        throw new Error('Authentication required');
      }

      const contentType = response.headers.get('content-type');
      if (contentType && contentType.includes('application/json')) {
        const data = await response.json();
        if (!response.ok) {
          throw new Error(data.detail || 'Request failed');
        }
        return data;
      } else {
        const text = await response.text();
        if (!response.ok) {
          throw new Error(text || 'Request failed');
        }
        return text;
      }
    } catch (err) {
      console.error(`API Error on ${endpoint}:`, err);
      throw err;
    }
  },

  get(endpoint) {
    return this.request(endpoint, { method: 'GET' });
  },

  post(endpoint, body) {
    return this.request(endpoint, {
      method: 'POST',
      body: body instanceof FormData ? body : JSON.stringify(body),
    });
  },

  put(endpoint, body) {
    return this.request(endpoint, {
      method: 'PUT',
      body: JSON.stringify(body),
    });
  },

  delete(endpoint) {
    return this.request(endpoint, { method: 'DELETE' });
  },
};

/* Moved from templates/realtime_api_calls.html. */
function updateTable() {
    const filter = document.getElementById('filterSelect').value;
    fetch(`/api_call_summary/api/latest_calls?filter=${filter}`)
        .then(response => response.json())
        .then(data => {
            const tableBody = document.querySelector('#apiCallsTable tbody');
            tableBody.innerHTML = '';
            data.forEach(call => {
                const row = `
                    <tr>
                        <td>${call.timestamp}</td>
                        <td>${call.domain}</td>
                        <td>${call.endpoint}</td>
                        <td>${call.method}</td>
                        <td>${call.status_code}</td>
                    </tr>
                `;
                tableBody.innerHTML += row;
            });
        });
}

document.getElementById('filterForm').addEventListener('submit', function(e) {
    e.preventDefault();
    updateTable();
});

// Update the table every second
setInterval(updateTable, 1000);

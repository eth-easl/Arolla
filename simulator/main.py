import sys
import os
import argparse
from simulator import plotting, scenarios

def main():
    parser = argparse.ArgumentParser(description="Run simulation scenarios.")
    parser.add_argument('-s', '--scenario', type=int, choices=[1, 2, 3, 4], default=1, help="Scenario to run: 1=Basic, 2=Client Count, 3=Server Budget, 4=Strategies+Budget+Capacity")
    
    # Common parameters
    parser.add_argument('-n', '--num_clients', type=int, default=100, help="Number of clients (default: 100)")
    parser.add_argument('-r', '--requests_per_client', type=int, default=500, help="Requests per client (default: 500)")
    parser.add_argument('-t', '--token_ratio', type=float, default=0.1, help="Token ratio (default: 0.1)")
    parser.add_argument('-b', '--bucket_size', type=int, default=10, help="Bucket size (default: 10)")
    parser.add_argument('--breaker_threshold', type=float, default=0.1, help="Circuit breaker threshold (default: 0.1)")
    parser.add_argument('--num_retries', type=int, default=3, help="Number of retries (default: 3)")
    
    # Scenario specific
    parser.add_argument('--client_counts', type=str, default="10,100,1000", help="Comma-separated client counts for scenario 2 (default: 10,100,1000)")
    parser.add_argument('--total_requests', type=int, default=50000, help="Total requests for scenario 2 (default: 50000)")
    parser.add_argument('--retry_budget_rate', type=float, default=0.1, help="Retry budget rate (default: 0.1)")
    parser.add_argument('--capacity', type=int, default=2000, help="Max capacity for scenario 4 (default: 2000)")

    args = parser.parse_args()
    #filenames be informative, including scenario name, parameters, etc. scenario is the subfolder name
    scenario_name = {1: "basic", 2: "client_count", 3: "server_budget", 4: "strategies_budget_capacity"}    
    filename = f"clients_{args.num_clients}_requests_{args.requests_per_client}_retry_budget_{args.retry_budget_rate}_capacity_{args.capacity}"
    filetype = ".png"
    output_dir = f"outputs/{scenario_name[args.scenario]}"
    os.makedirs(output_dir, exist_ok=True)
    
    plotting.setup_plotting()

    if args.scenario == 1:
        # 1. Basic Comparison
        x, results = scenarios.scenario_basic_comparison(
            num_clients=args.num_clients,
            requests_per_client=args.requests_per_client,
            token_ratio=args.token_ratio,
            bucket_size=args.bucket_size,
            breaker_threshold=args.breaker_threshold,
            num_retries=args.num_retries
        )
        
        colors_basic = {
            'no_retries': 'gray',
            'fixed_retries': 'cornflowerblue',
            'circuit_breaker': 'green',
            'token_bucket': 'orange',
        }
        
        res_success = {k: v[0] for k, v in results.items()}
        res_load = {k: v[1] for k, v in results.items()}

        output_path = os.path.join(output_dir, filename + '_success_rate' + filetype)
        print(f"Saving success rate plot to {output_path}")
        plotting.plot_generic(x, res_success, 'Server-side failure rate (%)', 'Successful Operations (%)', 
                              'Success Rate by Strategy', output_path, colors=colors_basic, xlim=(0, 10), ylim=(80, 102), legend_loc='best')
        output_path = os.path.join(output_dir, filename + '_load' + filetype)
        plotting.plot_generic(x, res_load, 'Server-side failure rate (%)', 'Load (%)', 
                              'Load by Strategy', output_path, colors=colors_basic, xlim=(0, 10), ylim=(99, 120), legend_loc='best')

    elif args.scenario == 2:
        # 2. Client Count
        try:
            client_counts = [int(x.strip()) for x in args.client_counts.split(',')]
        except ValueError:
            print("Invalid format for --client_counts. Using default [10, 100, 1000]")
            client_counts = [10, 100, 1000]

        x, results = scenarios.scenario_client_count(
            client_counts=client_counts,
            total_requests=args.total_requests,
            token_ratio=args.token_ratio,
            bucket_size=args.bucket_size,
            breaker_threshold=args.breaker_threshold,
            num_retries=args.num_retries
        )
        
        colors_cc = {}
        linestyles_cc = {}
        # Define styles
        style_map = {0: '-', 1: '--', 2: ':'} # solid, dashed, dotted
        
        for i, count in enumerate(client_counts):
            # Same color for same strategy
            colors_cc[f'token_bucket_{count}clients'] = 'orange'
            colors_cc[f'circuit_breaker_{count}clients'] = 'green'
            
            # Different linestyle for different client count
            ls = style_map.get(i % 3, '-')
            linestyles_cc[f'token_bucket_{count}clients'] = ls
            linestyles_cc[f'circuit_breaker_{count}clients'] = ls

        res_success = {k: v[0] for k, v in results.items()}
        res_load = {k: v[1] for k, v in results.items()}
        
        # Custom legend order: group same strategies together
        legend_order = []
        for strategy in ['token_bucket', 'circuit_breaker']:
            for count in client_counts:
                legend_order.append(f'{strategy}_{count}clients')
        
        output_path = os.path.join(output_dir, filename + '_success_rate' + filetype)   
        plotting.plot_generic(x, res_success, 'Server-side failure rate (%)', 'Successful Operations (%)',
                              'Client Count Effect (Success)', output_path, colors=colors_cc, linestyles=linestyles_cc, ylim=(80, 101), legend_loc='best', legend_order=legend_order)
        output_path = os.path.join(output_dir, filename + '_load' + filetype)
        plotting.plot_generic(x, res_load, 'Server-side failure rate (%)', 'Load (%)',
                              'Client Count Effect (Load)', output_path, colors=colors_cc, linestyles=linestyles_cc, ylim=(100, 120), legend_loc='best', legend_order=legend_order)
    else:
        raise ValueError(f"Invalid scenario: {args.scenario}")


if __name__ == "__main__":
    main()
